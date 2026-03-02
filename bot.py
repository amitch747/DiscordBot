import discord
from discord.ext import commands
from discord import Embed, File
import os
import aiohttp
import networkx as nx
from community import community_louvain
from pyvis.network import Network
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Data storage ──────────────────────────────────────────────

user_msg_counts = defaultdict(int)
user_names = {}
user_avatars = {}
edges = defaultdict(int)


def reset_data():
    user_msg_counts.clear()
    user_names.clear()
    user_avatars.clear()
    edges.clear()


# ── Scraping ──────────────────────────────────────────────────

async def scrape_channel(channel, status_msg):
    count = 0
    async for message in channel.history(limit=None):
        if message.author.bot:
            continue

        uid = message.author.id
        user_msg_counts[uid] += 1
        user_names[uid] = message.author.display_name
        user_avatars[uid] = str(message.author.display_avatar.url)

        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if hasattr(ref, "author") and not ref.author.bot:
                edges[(uid, ref.author.id)] += 1
                user_names[ref.author.id] = ref.author.display_name
                user_avatars[ref.author.id] = str(
                    ref.author.display_avatar.url)

        for mentioned in message.mentions:
            if not mentioned.bot and mentioned.id != uid:
                edges[(uid, mentioned.id)] += 1
                user_names[mentioned.id] = mentioned.display_name
                user_avatars[mentioned.id] = str(mentioned.display_avatar.url)

        count += 1
        if count % 2000 == 0:
            try:
                await status_msg.edit(
                    content=f"📡 Scanning **#{channel.name}**... {count:,} messages"
                )
            except discord.HTTPException:
                pass

    return count


# ── Graph building ────────────────────────────────────────────

def build_graph():
    G = nx.DiGraph()

    for uid, name in user_names.items():
        G.add_node(uid, label=name, size=user_msg_counts.get(uid, 0))

    for (src, dst), weight in edges.items():
        if src in G.nodes and dst in G.nodes:
            G.add_edge(src, dst, weight=weight)

    return G


def compute_stats(G):
    if len(G.nodes) == 0:
        return {}, {}, {}

    undirected = G.to_undirected()
    degree_cent = nx.degree_centrality(G)

    if len(undirected.nodes) > 1 and len(undirected.edges) > 0:
        communities = community_louvain.best_partition(undirected)
    else:
        communities = {n: 0 for n in undirected.nodes}

    relationship_weights = defaultdict(int)
    for (src, dst), weight in edges.items():
        key = tuple(sorted([src, dst]))
        relationship_weights[key] += weight

    return degree_cent, communities, relationship_weights


# ── Visualization ─────────────────────────────────────────────

COMMUNITY_COLORS = [
    "#5865F2", "#ED4245", "#57F287", "#FEE75C",
    "#EB459E", "#00D4AA", "#F47B67", "#9B59B6",
    "#3498DB", "#E67E22", "#1ABC9C", "#E74C3C",
]


async def download_avatar(session, url):
    try:
        small_url = url.replace("?size=1024", "?size=64")
        if "?" not in small_url:
            small_url += "?size=64"
        async with session.get(small_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                import base64
                data = await resp.read()
                b64 = base64.b64encode(data).decode()
                content_type = resp.content_type or "image/png"
                return f"data:{content_type};base64,{b64}"
    except Exception:
        pass
    return None


async def build_html(G, degree_cent, communities):
    net = Network(
        height="100vh",
        width="100%",
        bgcolor="#2C2F33",
        font_color="#FFFFFF",
        directed=True,
        select_menu=False,
        filter_menu=False,
    )

    net.barnes_hut(
        gravity=-8000,
        central_gravity=0.3,
        spring_length=200,
        spring_strength=0.01,
    )

    avatar_data = {}
    async with aiohttp.ClientSession() as session:
        tasks = {
            uid: download_avatar(session, url)
            for uid, url in user_avatars.items()
            if uid in G.nodes
        }
        for uid, task in tasks.items():
            avatar_data[uid] = await task

    max_msgs = max((user_msg_counts.get(uid, 1) for uid in G.nodes), default=1)

    for uid in G.nodes:
        name = user_names.get(uid, "Unknown")
        msgs = user_msg_counts.get(uid, 0)
        dc = degree_cent.get(uid, 0)
        comm = communities.get(uid, 0)
        color = COMMUNITY_COLORS[comm % len(COMMUNITY_COLORS)]

        size = 20 + (msgs / max_msgs) * 60

        title = (
            f"<b>{name}</b><br>"
            f"Messages: {msgs:,}<br>"
            f"Degree Centrality: {dc:.3f}<br>"
            f"Community: {comm}"
        )

        avatar_uri = avatar_data.get(uid)
        if avatar_uri:
            net.add_node(
                uid,
                label=name,
                title=title,
                size=size,
                shape="circularImage",
                image=avatar_uri,
                borderWidth=3,
                color={"border": color, "background": color},
            )
        else:
            net.add_node(
                uid,
                label=name,
                title=title,
                size=size,
                color=color,
            )

    max_weight = max((w for w in edges.values()), default=1)
    for (src, dst), weight in edges.items():
        if src in G.nodes and dst in G.nodes:
            width = 1 + (weight / max_weight) * 8
            net.add_edge(
                src, dst,
                value=weight,
                width=width,
                color={"color": "#99AAB5", "opacity": 0.6},
                title=f"{user_names.get(src, '?')} → {user_names.get(dst, '?')}: {weight} interactions",
            )

    output_path = "/tmp/echelon_graph.html"
    net.save_graph(output_path)

    with open(output_path, "r") as f:
        html = f.read()

    custom_css = """
    <style>
        body { margin: 0; overflow: hidden; background: #2C2F33; }
        #mynetwork { border: none !important; }
    </style>
    """
    html = html.replace("</head>", custom_css + "</head>")

    with open(output_path, "w") as f:
        f.write(html)

    return output_path


# ── Bot events ────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Echelon is online as {bot.user}")


# ── Commands ──────────────────────────────────────────────────

@bot.command(name="messagerank")
async def messagerank(ctx, channel: discord.TextChannel = None, limit: int = 10000):
    channel = channel or ctx.channel
    await ctx.send(f"📊 Scanning **#{channel.name}** (up to {limit:,} messages)...")

    counts = {}
    total = 0

    async for message in channel.history(limit=limit):
        if message.author.bot:
            continue
        counts[message.author.display_name] = counts.get(
            message.author.display_name, 0) + 1
        total += 1

    if not counts:
        await ctx.send("No messages found.")
        return

    sorted_users = sorted(counts.items(), key=lambda x: x[1], reverse=True)

    embed = Embed(
        title=f"📊 Message Rankings — #{channel.name}",
        description=f"Based on {total:,} messages",
        color=0x5865F2,
    )

    for i, (user, count) in enumerate(sorted_users[:15], 1):
        pct = (count / total) * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        embed.add_field(
            name=f"#{i} {user}",
            value=f"`{bar}` {count:,} ({pct:.1f}%)",
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="graph")
async def graph(ctx, *channels: discord.TextChannel):
    if channels:
        scan_channels = list(channels)
    else:
        scan_channels = [
            ch for ch in ctx.guild.text_channels
            if ch.permissions_for(ctx.guild.me).read_message_history
        ]

    reset_data()

    status_msg = await ctx.send(
        f"🔍 Starting scan of {len(scan_channels)} channel(s)..."
    )

    total_messages = 0
    for i, channel in enumerate(scan_channels, 1):
        try:
            await status_msg.edit(
                content=f"📡 Scanning channel {i}/{len(scan_channels)}: **#{channel.name}**..."
            )
            count = await scrape_channel(channel, status_msg)
            total_messages += count
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"Error scanning #{channel.name}: {e}")

    await status_msg.edit(
        content=f"✅ Scan complete: {total_messages:,} messages across {len(scan_channels)} channels. Building graph..."
    )

    G = build_graph()

    if len(G.nodes) < 2:
        await ctx.send("Not enough users to build a graph.")
        return

    degree_cent, communities, relationship_weights = compute_stats(G)

    embed = Embed(
        title="🌐 Echelon — Server Relationship Graph",
        description=f"**{len(G.nodes)}** users · **{len(G.edges)}** connections · **{total_messages:,}** messages",
        color=0x5865F2,
    )

    top_active = sorted(user_msg_counts.items(),
                        key=lambda x: x[1], reverse=True)[:5]
    active_text = "\n".join(
        f"**{i}.** {user_names.get(uid, '?')} — {count:,} msgs"
        for i, (uid, count) in enumerate(top_active, 1)
    )
    embed.add_field(name="📊 Most Active", value=active_text, inline=True)

    top_connected = sorted(degree_cent.items(),
                           key=lambda x: x[1], reverse=True)[:5]
    connected_text = "\n".join(
        f"**{i}.** {user_names.get(uid, '?')} — {dc:.3f}"
        for i, (uid, dc) in enumerate(top_connected, 1)
    )
    embed.add_field(name="🔗 Most Connected", value=connected_text, inline=True)

    top_rels = sorted(relationship_weights.items(),
                      key=lambda x: x[1], reverse=True)[:5]
    rel_text = "\n".join(
        f"**{i}.** {user_names.get(pair[0], '?')} ↔ {user_names.get(pair[1], '?')} — {w:,}"
        for i, (pair, w) in enumerate(top_rels, 1)
    )
    embed.add_field(name="💬 Strongest Relationships",
                    value=rel_text, inline=False)

    num_communities = len(set(communities.values()))
    comm_groups = defaultdict(list)
    for uid, comm in communities.items():
        comm_groups[comm].append(user_names.get(uid, "?"))

    comm_text = "\n".join(
        f"**Group {c + 1}** ({len(members)} members): {', '.join(members[:5])}{'...' if len(members) > 5 else ''}"
        for c, members in sorted(comm_groups.items())[:6]
    )
    embed.add_field(
        name=f"👥 Communities ({num_communities} found)",
        value=comm_text or "None detected",
        inline=False,
    )

    await ctx.send(embed=embed)

    await status_msg.edit(content="🎨 Generating interactive graph...")

    html_path = await build_html(G, degree_cent, communities)

    await ctx.send(
        content="🌐 **Interactive Graph** — download and open in your browser:",
        file=File(html_path, filename="echelon_graph.html"),
    )

    await status_msg.edit(content="✅ Done!")


bot.run(os.getenv("BOT_TOKEN"))

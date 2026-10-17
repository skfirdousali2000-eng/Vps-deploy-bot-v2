import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
from datetime import datetime
import shlex
import logging
import shutil
import os
from typing import Optional, List, Dict, Any
import threading
import time
import sqlite3
import random
import requests
import secrets
import string
import re
import ipaddress

# Load environment variables
def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Parse integer environment settings without crashing on malformed values."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logging.getLogger("zylexnode.config").warning(
            "Invalid integer for %s; using default %s", name, default
        )
        return default
    if value < minimum:
        logging.getLogger("zylexnode.config").warning(
            "%s must be >= %s; using default %s", name, minimum, default
        )
        return default
    return value

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
BOT_NAME = 'ZylexNode™'  # Fixed branding requested by owner
PREFIX = '1'  # Fixed command prefix requested by owner
YOUR_SERVER_IP = os.getenv('YOUR_SERVER_IP', '').strip()
# Optional per-node public IPv4 map, e.g. NODE_PUBLIC_IPS='{"1":"203.0.113.10"}'
try:
    NODE_PUBLIC_IPS = json.loads(os.getenv('NODE_PUBLIC_IPS', '{}'))
    if not isinstance(NODE_PUBLIC_IPS, dict):
        NODE_PUBLIC_IPS = {}
except (TypeError, ValueError):
    NODE_PUBLIC_IPS = {}

# ---- Public IPv4 detection and validation ----
_cached_public_ip = None

def _valid_ipv4(value: str) -> Optional[str]:
    try:
        addr = ipaddress.ip_address(str(value).strip())
        return str(addr) if isinstance(addr, ipaddress.IPv4Address) and not addr.is_loopback and not addr.is_unspecified else None
    except ValueError:
        return None

def get_public_ip(node_id: Optional[int] = None) -> str:
    """Return a configured/detected public IPv4, never silently report localhost.

    Set NODE_PUBLIC_IPS as JSON keyed by node id or node name for multi-node setups,
    or YOUR_SERVER_IP for a single public node. Auto-detection is only a fallback
    for the bot host and may not describe a remote node.
    """
    global _cached_public_ip
    if node_id is not None:
        node = get_node(node_id) if 'get_node' in globals() else None
        if node:
            for key in (str(node_id), node.get('name', '')):
                configured = _valid_ipv4(NODE_PUBLIC_IPS.get(key, ''))
                if configured:
                    return configured
            # A local node can use the bot host's configured public IP.
            if not node.get('is_local'):
                return _valid_ipv4(YOUR_SERVER_IP) or ''
    configured = _valid_ipv4(YOUR_SERVER_IP)
    if configured:
        return configured
    if _cached_public_ip:
        return _cached_public_ip
    for endpoint in ('https://api.ipify.org', 'https://ifconfig.me/ip'):
        try:
            resp = requests.get(endpoint, timeout=4)
            resp.raise_for_status()
            ip = _valid_ipv4(resp.text)
            if ip:
                _cached_public_ip = ip
                return ip
        except Exception as e:
            logger.warning("Public IPv4 lookup failed (%s): %s", endpoint, e)
    return ''

_raw_main_admin_ids = os.getenv('MAIN_ADMIN_ID', '1405866008127864852')
MAIN_ADMIN_IDS_ENV = [uid.strip() for uid in _raw_main_admin_ids.split(',') if uid.strip()]
MAIN_ADMIN_ID = int(MAIN_ADMIN_IDS_ENV[0]) if MAIN_ADMIN_IDS_ENV else 0  # backward-compatible display value
VPS_USER_ROLE_ID = _env_int('VPS_USER_ROLE_ID', 1210291131301101618)
DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'default')
BOT_VERSION = os.getenv('BOT_VERSION', '9.0-PRO')
BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'rehan.404')
BACKUP_GUILD_ID = _env_int('BACKUP_GUILD_ID', 1559236626062905416)
BACKUP_CHANNEL_ID = _env_int('BACKUP_CHANNEL_ID', 1559248597063569518)
DEFAULT_LOG_CHANNEL_ID = _env_int('LOG_CHANNEL_ID', 1559244531210264650)
DATABASE_PATH = os.path.abspath(os.getenv('DATABASE_PATH', 'vps.db'))
BACKUP_INTERVAL_SECONDS = max(300, _env_int('BACKUP_INTERVAL_SECONDS', 300, minimum=1))
BACKUP_DIR = os.getenv('BACKUP_DIR', '/var/backups/zylexnode')
BACKUP_RETENTION = max(2, _env_int('BACKUP_RETENTION', 12, minimum=1))
BACKUP_MAX_UPLOAD_MB = max(1, _env_int('BACKUP_MAX_UPLOAD_MB', 8, minimum=1))

# OS Options for VPS Creation and Reinstall
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS", "value": "ubuntu:20.04"},
    {"label": "Ubuntu 22.04 LTS", "value": "ubuntu:22.04"},
    {"label": "Ubuntu 24.04 LTS", "value": "ubuntu:24.04"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13"},
]

# Configure logging to file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# Database setup
def get_db():
    conn = sqlite3.connect(DATABASE_PATH if 'DATABASE_PATH' in globals() else os.path.abspath('vps.db'))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS main_admins (
        user_id TEXT PRIMARY KEY
    )''')
    for uid in MAIN_ADMIN_IDS_ENV:
        cur.execute('INSERT OR IGNORE INTO main_admins (user_id) VALUES (?)', (uid,))
    cur.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        location TEXT,
        total_vps INTEGER,
        tags TEXT DEFAULT '[]',
        api_key TEXT,
        url TEXT,
        is_local INTEGER DEFAULT 0
    )''')
    # Add local node if not exists
    cur.execute('SELECT COUNT(*) FROM nodes WHERE is_local = 1')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('IN', 'India', 100, '["india", "local"]', None, None, 1))  # Default India node
    else:
        # Migrate the existing default local node label without changing its ID or VPS assignments.
        cur.execute('SELECT id, name FROM nodes WHERE is_local = 1 ORDER BY id LIMIT 1')
        local_row = cur.fetchone()
        if local_row and local_row['name'] in (' Node', 'Node', 'Local', 'IN'):
            # Avoid UNIQUE conflicts if an unrelated node is already named IN.
            cur.execute('SELECT id FROM nodes WHERE name = ? AND id != ?', ('IN', local_row['id']))
            conflict = cur.fetchone()
            if not conflict:
                cur.execute("UPDATE nodes SET name = 'IN', location = 'India' WHERE id = ?", (local_row['id'],))
    # Built-in node profiles. IN is the actual local LXD host; US and LONDON
    # are pre-created remote slots and remain explicitly offline until a real
    # endpoint/API key is configured by an administrator. Never fake connectivity.
    for builtin_name, builtin_location, builtin_tags in (
        ('US', 'United States', ['us', 'remote']),
        ('LONDON', 'London, United Kingdom', ['london', 'uk', 'remote']),
    ):
        cur.execute('SELECT id FROM nodes WHERE lower(name) = lower(?)', (builtin_name,))
        if cur.fetchone() is None:
            cur.execute(
                'INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, 0)',
                (builtin_name, builtin_location, 100, json.dumps(builtin_tags), None, None),
            )
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        node_id INTEGER NOT NULL DEFAULT 1,
        container_name TEXT UNIQUE NOT NULL,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]',
        root_password TEXT DEFAULT '',
        expires_at TEXT DEFAULT NULL
    )''')
    # Migrations
    cur.execute('PRAGMA table_info(vps)')
    info = cur.fetchall()
    columns = [col[1] for col in info]
    if 'os_version' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'")
    if 'node_id' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1")
    if 'root_password' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN root_password TEXT DEFAULT ''")
    if 'expires_at' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN expires_at TEXT DEFAULT NULL")
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    settings_init = [
        ('cpu_threshold', '90'),
        ('ram_threshold', '90'),
    ]
    for key, value in settings_init:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def get_nodes() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes')
    rows = cur.fetchall()
    conn.close()
    nodes = [dict(row) for row in rows]
    for node in nodes:
        node['tags'] = json.loads(node['tags'])
    return nodes

def get_node(node_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        node = dict(row)
        node['tags'] = json.loads(node['tags'])
        return node
    return None

def get_current_vps_count(node_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM vps WHERE node_id = ?', (node_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall()
    conn.close()
    data = {}
    for row in rows:
        user_id = row['user_id']
        if user_id not in data:
            data[user_id] = []
        vps = dict(row)
        vps['shared_with'] = json.loads(vps['shared_with'])
        vps['suspension_history'] = json.loads(vps['suspension_history'])
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        data[user_id].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def get_main_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM main_admins')
    rows = cur.fetchall()
    conn.close()
    ids = [row['user_id'] for row in rows]
    return ids if ids else [str(MAIN_ADMIN_ID)]

def save_main_admins():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM main_admins')
    for uid in main_admin_ids:
        cur.execute('INSERT INTO main_admins (user_id) VALUES (?)', (uid,))
    conn.commit()
    conn.close()

def save_vps_data():
    conn = get_db()
    cur = conn.cursor()
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            shared_json = json.dumps(vps['shared_with'])
            history_json = json.dumps(vps['suspension_history'])
            suspended_int = 1 if vps['suspended'] else 0
            whitelisted_int = 1 if vps.get('whitelisted', False) else 0
            os_ver = vps.get('os_version', 'ubuntu:22.04')
            created_at = vps.get('created_at', datetime.now().isoformat())
            node_id = vps.get('node_id', 1)
            root_password = vps.get('root_password', '')
            expires_at = vps.get('expires_at')
            if 'id' not in vps or vps['id'] is None:
                cur.execute('''INSERT INTO vps (user_id, node_id, container_name, ram, cpu, storage, config, os_version, status, suspended, whitelisted, created_at, shared_with, suspension_history, root_password, expires_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int,
                             created_at, shared_json, history_json, root_password, expires_at))
                vps['id'] = cur.lastrowid
            else:
                cur.execute('''UPDATE vps SET user_id = ?, node_id = ?, container_name = ?, ram = ?, cpu = ?, storage = ?, config = ?, os_version = ?, status = ?, suspended = ?, whitelisted = ?, shared_with = ?, suspension_history = ?, root_password = ?, expires_at = ?
                               WHERE id = ?''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int, shared_json, history_json, root_password, expires_at, vps['id']))
    conn.commit()
    conn.close()

def save_admin_data():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM admins')
    for admin_id in admin_data['admins']:
        cur.execute('INSERT INTO admins (user_id) VALUES (?)', (admin_id,))
    conn.commit()
    conn.close()

# Port forwarding functions
def get_user_allocation(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT allocated_ports FROM port_allocations WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_used_ports(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0]

def allocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO port_allocations (user_id, allocated_ports) VALUES (?, COALESCE((SELECT allocated_ports FROM port_allocations WHERE user_id = ?), 0) + ?)', (user_id, user_id, amount))
    conn.commit()
    conn.close()

def deallocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE port_allocations SET allocated_ports = MAX(0, allocated_ports - ?) WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def get_available_host_port(node_id: int) -> Optional[int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT host_port FROM port_forwards WHERE vps_container IN (SELECT container_name FROM vps WHERE node_id = ?)', (node_id,))
    used_ports = set(row[0] for row in cur.fetchall())
    conn.close()
    for _ in range(100):
        port = random.randint(20000, 50000)
        if port not in used_ports:
            return port
    return None

async def create_port_forward(user_id: str, container: str, vps_port: int, node_id: int) -> Optional[int]:
    'Create TCP+UDP IPv4 forwards atomically; roll back partial LXD devices.'
    try:
        vps_port = int(vps_port)
    except (TypeError, ValueError):
        raise ValueError("VPS port must be an integer between 1 and 65535")
    if not 1 <= vps_port <= 65535:
        raise ValueError("VPS port must be between 1 and 65535")
    node = get_node(node_id)
    if not node:
        raise RuntimeError(f"Node {node_id} was not found")
    if not container or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", container):
        raise ValueError("Invalid LXD container name")

    # Retry a few times in case a port is already occupied by a non-bot service.
    last_error = None
    for _ in range(12):
        host_port = get_available_host_port(node_id)
        if not host_port:
            return None
        added = []
        try:
            # LXD proxy devices bind on the node's IPv4 wildcard; firewall and
            # upstream security rules must also allow this port.
            for proto in ("tcp", "udp"):
                device = f"{proto}_proxy_{host_port}"
                cmd = (f"config device add {shlex.quote(container)} {device} proxy "
                       f"listen={proto}:0.0.0.0:{host_port} "
                       f"connect={proto}:127.0.0.1:{vps_port}")
                await execute_lxc(container, cmd, node_id=node_id)
                added.append(device)
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute('''INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, created_at)
                               VALUES (?, ?, ?, ?, ?)''',
                            (str(user_id), container, vps_port, host_port, datetime.now().isoformat()))
                conn.commit()
            finally:
                conn.close()
            return host_port
        except Exception as e:
            last_error = e
            # Roll back any device created before a later operation failed.
            for device in reversed(added):
                try:
                    await execute_lxc(container, f"config device remove {shlex.quote(container)} {device}", node_id=node_id)
                except Exception as cleanup_error:
                    logger.error("Port-forward rollback failed for %s/%s: %s", container, device, cleanup_error)
            logger.warning("Port-forward attempt failed on node %s port %s: %s", node_id, host_port, e)
            # Retry only likely host-port collision/bind errors.
            err = str(e).lower()
            if not any(term in err for term in ("address already in use", "already exists", "bind", "listen")):
                break
    logger.error("Failed to create port forward for %s:%s: %s", container, vps_port, last_error)
    return None

async def remove_port_forward(forward_id: int, is_admin: bool = False) -> tuple[bool, Optional[str]]:
    conn = get_db()
    try:
        row = conn.execute('SELECT user_id, vps_container, host_port FROM port_forwards WHERE id = ?', (forward_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False, None
    user_id, container, host_port = row['user_id'], row['vps_container'], int(row['host_port'])
    node_id = find_node_id_for_container(container)
    if not node_id:
        logger.error("Cannot remove port forward %s: node for %s was not found", forward_id, container)
        return False, None
    errors = []
    # Remove both protocol devices independently; one missing device must not block cleanup.
    for proto in ("tcp", "udp"):
        try:
            await execute_lxc(container, f"config device remove {shlex.quote(container)} {proto}_proxy_{host_port}", node_id=node_id)
        except Exception as e:
            msg = str(e).lower()
            if not any(term in msg for term in ("not found", "doesn't exist", "does not exist", "no such device")):
                errors.append(f"{proto}: {e}")
    if errors:
        logger.error("Port forward %s partially removed; keeping DB record for repair: %s", forward_id, "; ".join(errors))
        return False, None
    conn = get_db()
    try:
        conn.execute('DELETE FROM port_forwards WHERE id = ?', (forward_id,))
        conn.commit()
    finally:
        conn.close()
    return True, user_id

def get_user_forwards(user_id: str) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM port_forwards WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]

async def recreate_port_forwards(container_name: str) -> int:
    """Repair stored forwards idempotently, counting only fully configured pairs."""
    node_id = find_node_id_for_container(container_name)
    if not node_id:
        logger.error("Cannot recreate forwards: node for %s was not found", container_name)
        return 0
    conn = get_db()
    try:
        rows = conn.execute('SELECT vps_port, host_port FROM port_forwards WHERE vps_container = ?', (container_name,)).fetchall()
    finally:
        conn.close()
    readded_count = 0
    for row in rows:
        vps_port, host_port = int(row['vps_port']), int(row['host_port'])
        ok = True
        for proto in ("tcp", "udp"):
            device = f"{proto}_proxy_{host_port}"
            try:
                # Replace stale device state deterministically. A missing device is fine.
                try:
                    await execute_lxc(container_name, f"config device remove {shlex.quote(container_name)} {device}", node_id=node_id)
                except Exception:
                    pass
                await execute_lxc(container_name,
                    f"config device add {shlex.quote(container_name)} {device} proxy listen={proto}:0.0.0.0:{host_port} connect={proto}:127.0.0.1:{vps_port}",
                    node_id=node_id)
            except Exception as e:
                ok = False
                logger.error("Failed to repair %s forward %s->%s for %s: %s", proto, host_port, vps_port, container_name, e)
        if ok:
            readded_count += 1
    return readded_count

def find_node_id_for_container(container_name: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT node_id FROM vps WHERE container_name = ?', (container_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 1  # Default to local

# Initialize database
init_db()

# Load data at startup
vps_data = get_vps_data()
admin_data = {'admins': get_admins()}
main_admin_ids = set(get_main_admins())

# Global settings from DB
CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Resource monitoring settings (logging only)
resource_monitor_active = True

# Helper function to truncate text
def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# Embed creation functions
def create_embed(title, description="", color=0x1a1a1a):
    embed = discord.Embed(
        title=truncate_text(f"🌟 {BOT_NAME} - {title}", 256),
        description=truncate_text(description, 4096),
        color=color
    )
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1551851022459346995/1551854053431181342/file_0000000012e482089bbcb6baf6541790.png?ex=6ab37c36&is=6ab22ab6&hm=befd08e6f0d32dc78d13de9f687071e0baafc9fe46c449b9dea196dc5a2214d2&")
    embed.set_footer(text=f"{BOT_NAME} VPS Manager v{BOT_VERSION} • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                     icon_url="https://cdn.discordapp.com/attachments/1551851022459346995/1551854054181707846/file_00000000d0208211a03e73df9a707fcb.png?ex=6ab37c36&is=6ab22ab6&hm=b1008fc48b2f4a57e89cdab46d804fcc53f53296e5a3f3401b94b5fff5ca0373&")
    return embed

def add_field(embed, name, value, inline=False):
    embed.add_field(
        name=truncate_text(f"▸ {name}", 256),
        value=truncate_text(value, 1024),
        inline=inline
    )
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x00ff88)

def create_error_embed(title, description=""):
    return create_embed(title, description, color=0xff3366)

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x00ccff)

def create_warning_embed(title, description=""):
    return create_embed(title, description, color=0xffaa00)

# Admin checks
def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id in main_admin_ids or user_id in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command. Contact support.")
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) in main_admin_ids:
            return True
        raise commands.CheckFailure("Only the main admin can use this command.")
    return commands.check(predicate)

# Check whether an LXD instance exists. Used to recover safely when `lxc init`
# times out after the daemon has already created the instance.
async def _lxc_instance_exists(container_name: str, node_id: int) -> bool:
    if not container_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", container_name):
        return False
    node = get_node(node_id)
    if not node:
        return False
    try:
        if node.get('is_local'):
            proc = await asyncio.create_subprocess_exec(
                'lxc', 'info', container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await asyncio.wait_for(proc.wait(), timeout=15) == 0
        response = await asyncio.to_thread(
            requests.post, f"{node['url']}/api/execute",
            json={"command": f"lxc info {shlex.quote(container_name)}"},
            params={"api_key": node["api_key"]}, timeout=15
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        return payload.get('returncode', 1) == 0
    except Exception as exc:
        logger.warning("Unable to verify LXD instance %s after init attempt: %s", container_name, exc)
        return False


# LXC command execution with multi-node support
async def execute_lxc(container_name: str, command: str, timeout=120, node_id: Optional[int] = None):
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    
    if not node:
        raise Exception(f"Node {node_id} not found")
    
    full_command = f"lxc {command}"
    
    if node['is_local']:
        try:
            cmd = shlex.split(full_command)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError(f"Command timed out after {timeout} seconds")
            
            if proc.returncode != 0:
                error = stderr.decode().strip() if stderr else "Command failed with no error output"
                if command.lstrip().startswith("init ") and await _lxc_instance_exists(container_name, node_id):
                    logger.warning("LXC init reported failure but instance %s exists; continuing recovery", container_name)
                    return True
                raise Exception(f"Local LXC command failed: {error}\nCommand: {full_command}")
            return stdout.decode().strip() if stdout else True
        except asyncio.TimeoutError as te:
            logger.error(f"LXC command timed out: {full_command} - {str(te)}")
            if command.lstrip().startswith("init ") and await _lxc_instance_exists(container_name, node_id):
                logger.warning("LXC init timed out but instance %s exists; continuing recovery", container_name)
                return True
            raise
        except Exception as e:
            logger.error(f"LXC Error: {full_command} - {str(e)}")
            raise
    else:
        url = f"{node['url']}/api/execute"
        data = {"command": full_command}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params, timeout=timeout)
            
            # Try to get detailed error information
            try:
                error_detail = response.json()
                if 'detail' in error_detail:
                    error_msg = error_detail['detail']
                elif 'error' in error_detail:
                    error_msg = error_detail['error']
                else:
                    error_msg = response.text
            except:
                error_msg = response.text
            
            response.raise_for_status()
            
            res = response.json()
            if res.get("returncode", 1) != 0:
                stderr = res.get("stderr", "Command failed")
                if command.lstrip().startswith("init ") and await _lxc_instance_exists(container_name, node_id):
                    logger.warning("Remote LXC init reported failure but instance %s exists; continuing recovery", container_name)
                    return True
                raise Exception(f"Remote LXC command failed on {node['name']}: {stderr}\nCommand: {full_command}")
            return res.get("stdout", True)
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Remote LXC error on node {node['name']} ({url}): {str(e)}")
            if command.lstrip().startswith("init ") and await _lxc_instance_exists(container_name, node_id):
                logger.warning("Remote LXC init request failed/timed out but instance %s exists; continuing recovery", container_name)
                return True
            # Include URL and status code if available
            if hasattr(e.response, 'status_code'):
                raise Exception(f"Remote execution failed on {node['name']}: HTTP {e.response.status_code} - {str(e)}")
            else:
                raise Exception(f"Remote execution failed on {node['name']}: {str(e)}")

async def safe_start_container(container_name: str, node_id: int):
    """Starts a container, treating 'already running' as a success instead of an error."""
    try:
        await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
    except Exception as e:
        err_text = str(e).lower()
        if "already running" in err_text or "is running" in err_text:
            logger.info(f"{container_name} was already running; treating start as success.")
        else:
            raise

# Apply LXC config
async def apply_lxc_config(container_name: str, node_id: int):
    try:
        await execute_lxc(container_name, f"config set {container_name} security.nesting true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.privileged true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.mknod true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.setxattr true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} linux.kernel_modules overlay,loop,nf_nat,ip_tables,ip6_tables,netlink_diag,br_netfilter", node_id=node_id)
        try:
            await execute_lxc(container_name, f"config device add {container_name} fuse unix-char path=/dev/fuse", node_id=node_id)
        except:
            pass
        raw_lxc_config = (
            "lxc.apparmor.profile = unconfined\n"
            "lxc.apparmor.allow_nesting = 1\n"
            "lxc.apparmor.allow_incomplete = 1\n"
            "\n"
            "lxc.cap.drop =\n"
            "lxc.cgroup.devices.allow = a\n"
            "lxc.cgroup2.devices.allow = a\n"
            "\n"
            "lxc.mount.auto = proc:rw sys:rw cgroup:rw shmounts:rw\n"
            "\n"
            "lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file 0 0\n"
        )
        await execute_lxc(container_name, f"config set {container_name} raw.lxc '{raw_lxc_config}'", node_id=node_id)
        logger.info(f"LXC permissions applied to {container_name} on node {node_id}")
    except Exception as e:
        logger.error(f"Failed to apply LXC config to {container_name}: {e}")

# Apply internal permissions
async def apply_internal_permissions(container_name: str, node_id: int):
    try:
        await asyncio.sleep(8)
        commands = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ping_group_range=0 2147483647' >> /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.d/99-custom.conf",
            "echo 'kernel.unprivileged_userns_clone=1' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true"
        ]
        for cmd in commands:
            try:
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id)
            except Exception as cmd_error:
                logger.warning(f"Command failed in {container_name}: {cmd} - {cmd_error}")
        logger.info(f"Internal permissions applied to {container_name}")
    except Exception as e:
        logger.error(f"Failed to apply internal permissions to {container_name}: {e}")

def generate_password(length: int = 16) -> str:
    """Generate a random alnum-only password (safe to embed in shell commands)."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

async def setup_ssh_access(container_name: str, node_id: int) -> str:
    """Installs openssh-server, sets a random root password, and enables
    password-based root SSH login inside the container. Returns the password."""
    password = generate_password()
    commands = [
        "DEBIAN_FRONTEND=noninteractive apt-get update -y",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server",
        f"echo 'root:{password}' | chpasswd",
        "printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n\\n' | cat - /etc/ssh/sshd_config > /tmp/sshd_config.new && mv /tmp/sshd_config.new /etc/ssh/sshd_config",
        "mkdir -p /run/sshd",
        "systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true",
        "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || service sshd restart 2>/dev/null || true"
    ]
    for cmd in commands:
        try:
            await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id, timeout=180)
        except Exception as cmd_error:
            logger.warning(f"SSH setup command failed in {container_name}: {cmd} - {cmd_error}")
    return password

# ---------------------------------------------------------------------------
# Pinggy.io SSH tunnel system (replaces local port-forwarding for VPS SSH)
# ---------------------------------------------------------------------------
PINGGY_LOG_PATH = "/root/.pinggy_tunnel.log"

def parse_pinggy_address(log_text: str) -> Optional[str]:
    """Extracts host:port from Pinggy's tcp:// forwarding line, stripping the tcp:// prefix."""
    if not log_text:
        return None
    match = re.search(r'tcp://([\w\.\-]+):(\d+)', log_text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return None

async def establish_pinggy_tunnel(container_name: str, node_id: int, retries: int = 4, wait_seconds: int = 5) -> Optional[str]:
    """
    Runs 'ssh -p 443 -R0:localhost:22 qr+tcp@free.pinggy.io' inside the container
    (equivalent to: lxc exec <container> -- bash, then running that ssh command and
    accepting the host-key prompt with 'yes' automatically), as a persistent background
    tunnel. Returns the assigned 'host:port' (with the tcp:// prefix stripped), or None
    if the address could not be determined.
    """
    try:
        # Make sure an SSH client exists inside the container
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"command -v ssh >/dev/null || (DEBIAN_FRONTEND=noninteractive apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-client)\"",
            node_id=node_id, timeout=180
        )
    except Exception as e:
        logger.warning(f"Pinggy: couldn't verify/install ssh client in {container_name}: {e}")

    # Kill any previous tunnel and clear the old log
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"pkill -f 'free.pinggy.io' >/dev/null 2>&1; rm -f {PINGGY_LOG_PATH}\"",
            node_id=node_id
        )
    except Exception:
        pass

    # Start the tunnel in the background. -o StrictHostKeyChecking=no auto-accepts the
    # host key prompt (the 'always yes' behavior), so it never blocks waiting for input.
    tunnel_cmd = (
        "setsid nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o ServerAliveInterval=30 -R0:localhost:22 qr+tcp@free.pinggy.io "
        f"> {PINGGY_LOG_PATH} 2>&1 < /dev/null & disown"
    )
    try:
        await execute_lxc(
            container_name,
            f"exec {container_name} -- bash -c \"{tunnel_cmd}\"",
            node_id=node_id
        )
    except Exception as e:
        logger.error(f"Pinggy: failed to start tunnel in {container_name}: {e}")
        return None

    # Poll the log until the tcp:// address shows up
    for attempt in range(retries):
        await asyncio.sleep(wait_seconds)
        try:
            log_output = await execute_lxc(
                container_name,
                f"exec {container_name} -- cat {PINGGY_LOG_PATH}",
                node_id=node_id
            )
        except Exception:
            log_output = ""
        address = parse_pinggy_address(log_output if isinstance(log_output, str) else "")
        if address:
            return address
    logger.warning(f"Pinggy: no tunnel address found for {container_name} after {retries} attempts")
    return None

# Get or create VPS role
async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID

    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return None

    role_name = f"{BOT_NAME} VPS User"

    # Try cached role
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role and role < me.top_role:
            return role
        VPS_USER_ROLE_ID = None

    # Find by name
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        if role >= me.top_role:
            try:
                await role.delete(reason="Role above bot, recreating")
            except discord.Forbidden:
                return None
            role = None
        else:
            VPS_USER_ROLE_ID = role.id
            return role

    # Create safely below bot
    try:
        role = await guild.create_role(
            name=role_name,
            color=discord.Color.dark_purple(),
            permissions=discord.Permissions.none(),
            reason=f"{BOT_NAME} VPS User role"
        )
        await role.edit(position=me.top_role.position - 1)
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS role: {role.id}")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS role: {e}")
        return None

# Host resource functions
def get_host_cpu_usage():
    try:
        if shutil.which("mpstat"):
            result = subprocess.run(['mpstat', '1', '1'], capture_output=True, text=True)
            output = result.stdout
            for line in output.split('\n'):
                if 'all' in line and '%' in line:
                    parts = line.split()
                    idle = float(parts[-1])
                    return 100.0 - idle
        else:
            result = subprocess.run(['top', '-bn1'], capture_output=True, text=True)
            output = result.stdout
            for line in output.split('\n'):
                if '%Cpu(s):' in line:
                    parts = line.split()
                    us = float(parts[1])
                    sy = float(parts[3])
                    ni = float(parts[5])
                    id_ = float(parts[7])
                    wa = float(parts[9])
                    hi = float(parts[11])
                    si = float(parts[13])
                    st = float(parts[15])
                    usage = us + sy + ni + wa + hi + si + st
                    return usage
        return 0.0
    except Exception as e:
        logger.error(f"Error getting CPU usage: {e}")
        return 0.0

def get_host_ram_usage():
    try:
        result = subprocess.run(['free', '-m'], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            mem = lines[1].split()
            total = int(mem[1])
            used = int(mem[2])
            return (used / total * 100) if total > 0 else 0.0
        return 0.0
    except Exception as e:
        logger.error(f"Error getting RAM usage: {e}")
        return 0.0

async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {"cpu": get_host_cpu_usage(), "ram": get_host_ram_usage()}
    else:
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get host stats from node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0}

def resource_monitor():
    global resource_monitor_active
    backup_interval = 3600
    last_backup = time.time()
    while resource_monitor_active:
        try:
            nodes = get_nodes()
            for node in nodes:
                stats = asyncio.run(get_host_stats(node['id']))
                cpu = stats['cpu']
                ram = stats['ram']
                logger.info(f"Node {node['name']}: CPU {cpu:.1f}%, RAM {ram:.1f}%")
                if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
                    logger.warning(f"Node {node['name']} exceeded thresholds (CPU: {CPU_THRESHOLD}%, RAM: {RAM_THRESHOLD}%). Manual intervention required.")
            if time.time() - last_backup > backup_interval:
                backup_name = f"vps_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                try:
                    shutil.copy('vps.db', backup_name)
                    if os.path.exists('vps.db-wal'):
                        shutil.copy('vps.db-wal', f"{backup_name}-wal")
                    if os.path.exists('vps.db-shm'):
                        shutil.copy('vps.db-shm', f"{backup_name}-shm")
                    logger.info(f"Database backup created: {backup_name}")
                    last_backup = time.time()
                except Exception as e:
                    logger.error(f"Failed to create DB backup: {e}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Error in resource monitor: {e}")
            time.sleep(60)

# Start resource monitoring thread
monitor_thread = threading.Thread(target=resource_monitor, daemon=True)
monitor_thread.start()

# Container stats with multi-node
async def get_container_stats(container_name: str, node_id: Optional[int] = None) -> Dict:
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if node['is_local']:
        status = await get_container_status_local(container_name)
        cpu = await get_container_cpu_pct_local(container_name)
        ram = await get_container_ram_local(container_name)
        disk = await get_container_disk_local(container_name)
        uptime = await get_container_uptime_local(container_name)
        return {"status": status, "cpu": cpu, "ram": ram, "disk": disk, "uptime": uptime}
    else:
        url = f"{node['url']}/api/get_container_stats"
        data = {"container": container_name}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get container stats from node {node['name']}: {e}")
            return {"status": "unknown", "cpu": 0.0, "ram": {"used": 0, "total": 0, "pct": 0.0}, "disk": "Unknown", "uptime": "Unknown"}

async def get_container_status_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "info", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.splitlines():
            if line.startswith("Status: "):
                return line.split(": ", 1)[1].strip().lower()
        return "unknown"
    except Exception:
        return "unknown"

async def get_container_cpu_pct_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "top", "-bn1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.splitlines():
            if '%Cpu(s):' in line:
                parts = line.split()
                us = float(parts[1])
                sy = float(parts[3])
                ni = float(parts[5])
                id_ = float(parts[7])
                wa = float(parts[9])
                hi = float(parts[11])
                si = float(parts[13])
                st = float(parts[15])
                return us + sy + ni + wa + hi + si + st
        return 0.0
    except Exception as e:
        logger.error(f"Error getting CPU for {container_name}: {e}")
        return 0.0

async def get_container_ram_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "free", "-m",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            total = int(parts[1])
            used = int(parts[2])
            pct = (used / total * 100) if total > 0 else 0.0
            return {'used': used, 'total': total, 'pct': pct}
        return {'used': 0, 'total': 0, 'pct': 0.0}
    except Exception as e:
        logger.error(f"Error getting RAM for {container_name}: {e}")
        return {'used': 0, 'total': 0, 'pct': 0.0}

async def get_container_disk_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "df", "-h", "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        for line in lines:
            if '/dev/' in line and ' /' in line:
                parts = line.split()
                if len(parts) >= 5:
                    used = parts[2]
                    size = parts[1]
                    perc = parts[4]
                    return f"{used}/{size} ({perc})"
        return "Unknown"
    except Exception:
        return "Unknown"

async def get_container_uptime_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "uptime",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if stdout else "Unknown"
    except Exception:
        return "Unknown"

async def get_container_status(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['status']

async def get_container_cpu(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return f"{stats['cpu']:.1f}%"

async def get_container_cpu_pct(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['cpu']

async def get_container_memory(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    ram = stats['ram']
    return f"{ram['used']}/{ram['total']} MB ({ram['pct']:.1f}%)"

async def get_container_ram_pct(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['ram']['pct']

async def get_container_disk(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['disk']

async def get_container_uptime(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['uptime']

def get_uptime():
    try:
        result = subprocess.run(['uptime'], capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return "Unknown"

# Try to detect default storage pool or use common defaults
def get_default_storage_pool():
    try:
        result = subprocess.run(['lxc', 'storage', 'list', '--format', 'csv'], 
                              capture_output=True, text=True)
        lines = result.stdout.strip().split('\n')
        if lines and lines[0]:
            # Get first storage pool
            return lines[0].split(',')[0]
    except:
        pass
    return "default"  # Fallback to 'default'

DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', get_default_storage_pool())

# Bot events
@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ZylexNode™ VPS MANAGER"))
    if not automatic_metadata_backup.is_running():
        automatic_metadata_backup.change_interval(seconds=BACKUP_INTERVAL_SECONDS)
        automatic_metadata_backup.start()
    if not automatic_vps_health_check.is_running():
        automatic_vps_health_check.start()
    logger.info(f"{BOT_NAME} Bot is ready!")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", f"Please check command usage with `{PREFIX}help`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        error_msg = str(error) if str(error) else "You need admin permissions for this command. Contact support."
        await ctx.send(embed=create_error_embed("Access Denied", error_msg))
    elif isinstance(error, discord.NotFound):
        await ctx.send(embed=create_error_embed("Error", "The requested resource was not found. Please try again."))
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An unexpected error occurred. Support has been notified."))

# Bot commands
@bot.command(name='ping')
async def ping(ctx):
    """Show websocket latency, SQLite query latency, and a simple health status."""
    import time as _time

    websocket_ms = max(0.0, bot.latency * 1000.0)
    database_ms = None
    database_error = None
    started = _time.perf_counter()
    try:
        conn = get_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        database_ms = (_time.perf_counter() - started) * 1000.0
    except Exception as exc:
        logger.exception("Ping command database health check failed")
        database_error = type(exc).__name__

    # Keep the status descriptive; this is a lightweight diagnostic, not a full host test.
    if database_error:
        status = "🔴 Database check failed"
        color = 0xE74C3C
    elif websocket_ms >= 500 or database_ms >= 250:
        status = "<:bad_signal:1555957527295041636> High latency"
        color = 0xE74C3C
    elif websocket_ms >= 200 or database_ms >= 100:
        status = "🟠 Moderate latency"
        color = 0xF39C12
    else:
        status = "🟢 Healthy"
        color = 0x2ECC71

    embed = discord.Embed(
        title=f"<:stats:1555957645364564068> System Latency Report",
        color=color,
    )
    embed.add_field(
        name="<:arrow_right:1555957522999808101> Connection",
        value=f"**Websocket**\n`{websocket_ms:.2f}ms`",
        inline=False,
    )
    database_value = f"`{database_ms:.2f}ms`" if database_ms is not None else f"`Error: {database_error}`"
    embed.add_field(
        name="<:arrow_right:1555957522999808101> Database",
        value=f"**Database**\n{database_value}",
        inline=False,
    )
    embed.add_field(
        name="<:arrow_right:1555957522999808101> Status",
        value=status,
        inline=False,
    )
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1551851022459346995/1551854053431181342/file_0000000012e482089bbcb6baf6541790.png")
    embed.set_footer(text=f"{BOT_NAME} VPS Manager v{BOT_VERSION} • Lightweight diagnostic")
    await ctx.send(embed=embed)

@bot.command(name='uptime')
async def uptime(ctx):
    up = get_uptime()
    embed = create_info_embed("Host Uptime", up)
    await ctx.send(embed=embed)

@bot.command(name='thresholds')
@is_admin()
async def thresholds(ctx):
    embed = create_info_embed("Resource Thresholds", f"**CPU:** {CPU_THRESHOLD}%\n**RAM:** {RAM_THRESHOLD}%")
    await ctx.send(embed=embed)

@bot.command(name='set-threshold')
@is_admin()
async def set_threshold(ctx, cpu: int, ram: int):
    global CPU_THRESHOLD, RAM_THRESHOLD
    if cpu < 0 or ram < 0:
        await ctx.send(embed=create_error_embed("Invalid Thresholds", "Thresholds must be non-negative."))
        return
    CPU_THRESHOLD = cpu
    RAM_THRESHOLD = ram
    set_setting('cpu_threshold', str(cpu))
    set_setting('ram_threshold', str(ram))
    embed = create_success_embed("Thresholds Updated", f"**CPU:** {cpu}%\n**RAM:** {ram}%")
    await ctx.send(embed=embed)

@bot.command(name='set-status')
@is_admin()
async def set_status(ctx, activity_type: str, *, name: str):
    types = {
        'playing': discord.ActivityType.playing,
        'watching': discord.ActivityType.watching,
        'listening': discord.ActivityType.listening,
        'streaming': discord.ActivityType.streaming,
    }
    if activity_type.lower() not in types:
        await ctx.send(embed=create_error_embed("Invalid Type", "Valid types: playing, watching, listening, streaming"))
        return
    await bot.change_presence(activity=discord.Activity(type=types[activity_type.lower()], name=name))
    embed = create_success_embed("Status Updated", f"Set to {activity_type}: {name}")
    await ctx.send(embed=embed)

@bot.command(name="myvps")
async def my_vps(ctx):
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])

    # ─── No VPS Case ───────────────────────────────────────────
    if not vps_list:
        embed = create_error_embed(
            "❌ No VPS Found",
            f"You don’t have any **{BOT_NAME} VPS** yet."
        )
        embed.add_field(
            name="🚀 Quick Actions",
            value=(
                f"• `{PREFIX}manage` – Manage VPS\n"
                f"• Contact an admin to request a VPS"
            ),
            inline=False
        )
        await ctx.send(embed=embed)
        return

    # ─── Embed ────────────────────────────────────────────────
    embed = create_info_embed(
        title="🖥️ My VPS Dashboard",
        description="Your personal VPS overview"
    )

    total_vps = len(vps_list)
    running = suspended = whitelisted = 0
    vps_cards = []

    # ─── VPS Processing ───────────────────────────────────────
    for i, vps in enumerate(vps_list, start=1):
        node = get_node(vps.get("node_id"))
        node_name = node["name"] if node else "Unknown"

        config = vps.get("config", "Custom")
        ram = vps.get("ram", "0GB")
        cpu = vps.get("cpu", "0")
        storage = vps.get("storage", "0GB")

        if vps.get("suspended"):
            status = "⛔ SUSPENDED"
            suspended += 1
        elif vps.get("status") == "running":
            status = "🟢 RUNNING"
            running += 1
        else:
            status = "🔴 STOPPED"

        if vps.get("whitelisted"):
            whitelisted += 1

        vps_cards.append(
            f"**{i}.** `{vps['container_name']}`\n"
            f"{status} • `{config}`\n"
            f"⚙️ `{ram}` RAM • `{cpu}` CPU • `{storage}` Disk\n"
            f"📍 Node: `{node_name}`"
        )

    # ─── Row 1 : Summary ──────────────────────────────────────
    embed.add_field(
        name="📊 Summary",
        value=(
            f"🖥️ `{total_vps}` VPS\n"
            f"🟢 `{running}` Running\n"
            f"⛔ `{suspended}` Suspended\n"
            f"✅ `{whitelisted}` Whitelisted"
        ),
        inline=True
    )

    embed.add_field(
        name="⚡ Quick Actions",
        value=(
            f"`{PREFIX}manage`\n"
            f"`{PREFIX}reinstall`\n"
            f"`{PREFIX}status`"
        ),
        inline=True
    )

    embed.add_field(
        name="🧭 Tip",
        value="Use **manage** to control your VPS",
        inline=True
    )

    # ─── VPS Cards (Full Width) ───────────────────────────────
    vps_text = "\n\n".join(vps_cards)
    for i in range(0, len(vps_text), 1024):
        embed.add_field(
            name="🖥️ Your VPS",
            value=vps_text[i:i + 1024],
            inline=False
        )

    embed.set_footer(text=f"{BOT_NAME} • VPS Control Panel")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name='lxc-list')
@is_admin()
async def lxc_list(ctx, node_id: int = 1):
    try:
        result = await execute_lxc("", "list", node_id=node_id)
        node = get_node(node_id)
        embed = create_info_embed(f"LXC Containers List on {node['name']}", result)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))

class NodeSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        nodes = get_nodes()
        options = []
        # Admin capacity bypass: node.total_vps is a normal-user scheduling limit,
        # not a hard limit for administrators. Host resources still apply.
        requester_id = str(ctx.author.id)
        requester_is_admin = (
            requester_id in {str(x) for x in main_admin_ids}
            or requester_id in {str(x) for x in admin_data.get("admins", [])}
        )
        for n in nodes:
            current_count = get_current_vps_count(n['id'])
            capacity = int(n.get('total_vps') or 0)
            if requester_is_admin or current_count < capacity:
                remaining = max(0, capacity - current_count)
                description = f"{n.get('location') or 'Unknown'} - " + ("Admin capacity bypass" if requester_is_admin else f"Available: {remaining}")
                options.append(discord.SelectOption(label=n['name'], value=str(n['id']), description=description[:100]))
        if not options:
            self.add_item(discord.ui.Select(placeholder="No available nodes", disabled=True))
        else:
            self.select = discord.ui.Select(placeholder="Select a Node for the VPS", options=options)
            self.select.callback = self.select_node
            self.add_item(self.select)

    async def select_node(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        node_id = int(self.select.values[0])
        self.select.disabled = True
        await interaction.response.edit_message(view=self)
        os_view = OSSelectView(self.ram, self.cpu, self.disk, self.user, self.ctx, node_id)
        await interaction.followup.send(embed=create_info_embed("Select OS", "Choose the OS for the VPS."), view=os_view)

class OSSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx, node_id: int):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        self.node_id = node_id
        self.select = discord.ui.Select(
            placeholder="Select an OS for the VPS",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        os_version = self.select.values[0]
        self.select.disabled = True
        creating_embed = create_info_embed("Creating VPS", f"Deploying {os_version} VPS for {self.user.mention} on node {self.node_id}...")
        await interaction.response.edit_message(embed=creating_embed, view=self)
        user_id = str(self.user.id)
        if user_id not in vps_data:
            vps_data[user_id] = []
        vps_count = len(vps_data[user_id]) + 1
        container_name = f"{BOT_NAME.lower()}-vps-{user_id}-{vps_count}"
        ram_mb = self.ram * 1024
        try:
            await execute_lxc(container_name, f"init {os_version} {container_name} -s {DEFAULT_STORAGE_POOL}", timeout=300, node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.memory {ram_mb}MB", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {self.cpu}", node_id=self.node_id)
            await execute_lxc(container_name, f"config device set {container_name} root size={self.disk}GB", node_id=self.node_id)
            await apply_lxc_config(container_name, self.node_id)
            await safe_start_container(container_name, self.node_id)
            await apply_internal_permissions(container_name, self.node_id)
            root_password = await setup_ssh_access(container_name, self.node_id)
            pinggy_address = await establish_pinggy_tunnel(container_name, self.node_id)
            config_str = f"{self.ram}GB RAM / {self.cpu} CPU / {self.disk}GB Disk"
            vps_info = {
                "container_name": container_name,
                "node_id": self.node_id,
                "ram": f"{self.ram}GB",
                "cpu": str(self.cpu),
                "storage": f"{self.disk}GB",
                "config": config_str,
                "os_version": os_version,
                "status": "running",
                "suspended": False,
                "whitelisted": False,
                "suspension_history": [],
                "created_at": datetime.now().isoformat(),
                "shared_with": [],
                "root_password": root_password,
                "pinggy_address": pinggy_address,
                "id": None
            }
            vps_data[user_id].append(vps_info)
            save_vps_data()
            if self.ctx.guild:
                vps_role = await get_or_create_vps_role(self.ctx.guild)
                if vps_role:
                    try:
                        await self.user.add_roles(vps_role, reason=f"{BOT_NAME} VPS ownership granted")
                    except discord.Forbidden:
                        logger.warning(f"Failed to assign VPS role to {self.user.name}")
            success_embed = create_success_embed("VPS Created Successfully")
            add_field(success_embed, "Owner", self.user.mention, True)
            add_field(success_embed, "VPS ID", f"#{vps_count}", True)
            add_field(success_embed, "Container", f"`{container_name}`", True)
            add_field(success_embed, "Node", get_node(self.node_id)['name'], True)
            add_field(success_embed, "Resources", f"**RAM:** {self.ram}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.disk}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
            add_field(success_embed, "Disk Note", "Run `sudo resize2fs /` inside VPS if needed to expand filesystem.", False)
            await interaction.followup.send(embed=success_embed)
            dm_embed = create_success_embed("VPS Created!", f"Your VPS has been successfully deployed by an admin!")
            add_field(dm_embed, "VPS Details", f"**VPS ID:** #{vps_count}\n**Container Name:** `{container_name}`\n**Configuration:** {config_str}\n**Status:** Running\n**OS:** {os_version}\n**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", False)
            if pinggy_address:
                tunnel_host, tunnel_port = pinggy_address.split(":")
                add_field(dm_embed, "🔑 SSH Login", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n**Password:** `{root_password}`\n\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
            else:
                add_field(dm_embed, "🔑 Root Password", f"`{root_password}`\n⚠️ SSH tunnel setup failed — use `{PREFIX}manage` → 🔌 Reconnect Tunnel to try again.", False)
            add_field(dm_embed, "Management", f"• Use `{PREFIX}manage` to start/stop/reinstall your VPS\n• Use `{PREFIX}manage` → SSH for terminal access\n• Contact admin for upgrades or issues", False)
            add_field(dm_embed, "Important Notes", "• Full root access via SSH\n• Docker-ready with nesting and privileged mode\n• Back up your data regularly\n• Change your root password after first login", False)
            try:
                await self.user.send(embed=dm_embed)
            except discord.Forbidden:
                await self.ctx.send(embed=create_info_embed("Notification Failed", f"Couldn't send DM to {self.user.mention}. Please ensure DMs are enabled."))
        except Exception as e:
            error_embed = create_error_embed("Creation Failed", f"Error: {str(e)}")
            await interaction.followup.send(embed=error_embed)

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member):
    if user.bot:
        await ctx.send(embed=create_error_embed("Invalid Owner", "A VPS cannot be assigned to a bot account."))
        return
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs", "RAM, CPU, and Disk must be positive integers."))
        return
    if ram > 1024 or cpu > 256 or disk > 10000:
        await ctx.send(embed=create_error_embed("Resource Limit", "Requested limits exceed safety caps (RAM 1024 GB, CPU 256 cores, disk 10000 GB). Configure host capacity before requesting larger VPSs."))
        return
    if ctx.guild is None:
        await ctx.send(embed=create_error_embed("Run in a Server Channel", f"`{PREFIX}create {ram} {cpu} {disk} @user` must be sent in a text channel inside your Discord server, not in a DM with the bot. Open your server's VPS channel and run the command there."))
        return
    embed = create_info_embed("VPS Creation", f"Creating VPS for {user.mention} with {ram}GB RAM, {cpu} CPU cores, {disk}GB Disk.\nSelect a node below.")
    view = NodeSelectView(ram, cpu, disk, user, ctx)
    await ctx.send(embed=embed, view=view)

class ReinstallOSSelectView(discord.ui.View):
    def __init__(self, parent_view, container_name, owner_id, actual_idx, ram_gb, cpu, storage_gb, node_id):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.container_name = container_name
        self.owner_id = owner_id
        self.actual_idx = actual_idx
        self.ram_gb = ram_gb
        self.cpu = cpu
        self.storage_gb = storage_gb
        self.node_id = node_id
        self.select = discord.ui.Select(
            placeholder="Select an OS for the reinstall",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        os_version = self.select.values[0]
        self.select.disabled = True
        creating_embed = create_info_embed("Reinstalling VPS", f"Deploying {os_version} for `{self.container_name}`...")
        await interaction.response.edit_message(embed=creating_embed, view=self)
        ram_mb = self.ram_gb * 1024
        try:
            # No need to delete again; already deleted in confirmation
            await execute_lxc(self.container_name, f"init {os_version} {self.container_name} -s {DEFAULT_STORAGE_POOL}", timeout=300, node_id=self.node_id)
            await execute_lxc(self.container_name, f"config set {self.container_name} limits.memory {ram_mb}MB", node_id=self.node_id)
            await execute_lxc(self.container_name, f"config set {self.container_name} limits.cpu {self.cpu}", node_id=self.node_id)
            await execute_lxc(self.container_name, f"config device set {self.container_name} root size={self.storage_gb}GB", node_id=self.node_id)
            await apply_lxc_config(self.container_name, self.node_id)
            await execute_lxc(self.container_name, f"start {self.container_name}", node_id=self.node_id)
            await apply_internal_permissions(self.container_name, self.node_id)
            root_password = await setup_ssh_access(self.container_name, self.node_id)
            pinggy_address = await establish_pinggy_tunnel(self.container_name, self.node_id)
            target_vps = vps_data[self.owner_id][self.actual_idx]
            target_vps["os_version"] = os_version
            target_vps["status"] = "running"
            target_vps["suspended"] = False
            target_vps["created_at"] = datetime.now().isoformat()
            target_vps["root_password"] = root_password
            target_vps["pinggy_address"] = pinggy_address
            config_str = f"{self.ram_gb}GB RAM / {self.cpu} CPU / {self.storage_gb}GB Disk"
            target_vps["config"] = config_str
            save_vps_data()
            success_embed = create_success_embed("Reinstall Complete", f"VPS `{self.container_name}` has been successfully reinstalled!")
            add_field(success_embed, "Resources", f"**RAM:** {self.ram_gb}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.storage_gb}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
            add_field(success_embed, "Disk Note", "Run `sudo resize2fs /` inside VPS if needed to expand filesystem.", False)
            await interaction.followup.send(embed=success_embed, ephemeral=True)
            try:
                owner_user = await bot.fetch_user(int(self.owner_id))
                dm_embed = create_success_embed("VPS Reinstalled!", f"Your VPS `{self.container_name}` was reinstalled with a new OS. Your SSH password has changed.")
                if pinggy_address:
                    tunnel_host, tunnel_port = pinggy_address.split(":")
                    add_field(dm_embed, "🔑 SSH Login", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n**Password:** `{root_password}`\n\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
                else:
                    add_field(dm_embed, "🔑 Root Password", f"`{root_password}`\n⚠️ SSH tunnel setup failed — use `{PREFIX}manage` → 🔌 Reconnect Tunnel to try again.", False)
                await owner_user.send(embed=dm_embed)
            except Exception:
                pass
            self.stop()
        except Exception as e:
            error_embed = create_error_embed("Reinstall Failed", f"Error: {str(e)}")
            await interaction.followup.send(embed=error_embed, ephemeral=True)
            self.stop()

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False, actual_index: Optional[int] = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list[:]
        self.selected_index = None
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin
        self.actual_index = actual_index
        self.indices = list(range(len(vps_list)))
        if self.is_shared and self.actual_index is None:
            raise ValueError("actual_index required for shared views")
        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('config', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = create_embed("VPS Management", "Select a VPS from the dropdown menu below.", 0x1a1a1a)
            add_field(self.initial_embed, "Available VPS", "\n".join([f"**VPS {i+1}:** `{v['container_name']}` - Status: `{v.get('status', 'unknown').upper()}`" for i, v in enumerate(vps_list)]), False)
        else:
            self.selected_index = 0
            self.initial_embed = None
            self.add_action_buttons()

    async def get_initial_embed(self):
        if self.initial_embed is not None:
            return self.initial_embed
        self.initial_embed = await self.create_vps_embed(self.selected_index)
        return self.initial_embed

    async def create_vps_embed(self, index):
        vps = self.vps_list[index]
        node = get_node(vps['node_id'])
        node_name = node['name'] if node else "Unknown"
        status = vps.get('status', 'unknown')
        suspended = vps.get('suspended', False)
        whitelisted = vps.get('whitelisted', False)
        status_color = 0x00ff88 if status == 'running' and not suspended else 0xffaa00 if suspended else 0xff3366
        container_name = vps['container_name']
        stats = await get_container_stats(container_name)
        status_text = f"{stats['status'].upper()}"
        if suspended:
            status_text += " (SUSPENDED)"
        if whitelisted:
            status_text += " (WHITELISTED)"
        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = await bot.fetch_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {owner_user.mention}"
            except:
                owner_text = f"\n**Owner ID:** {self.owner_id}"
        embed = create_embed(
            f"VPS Management - VPS {index + 1}",
            f"Managing container: `{container_name}` on node {node_name}{owner_text}",
            status_color
        )
        resource_info = f"**Configuration:** {vps.get('config', 'Custom')}\n"
        resource_info += f"**Status:** `{status_text}`\n"
        resource_info += f"**RAM:** {vps['ram']}\n"
        resource_info += f"**CPU:** {vps['cpu']} Cores\n"
        resource_info += f"**Storage:** {vps['storage']}\n"
        resource_info += f"**OS:** {vps.get('os_version', 'ubuntu:22.04')}\n"
        resource_info += f"**Uptime:** {stats['uptime']}"
        add_field(embed, "📊 Allocated Resources", resource_info, False)
        if suspended:
            add_field(embed, "⚠️ Suspended", "This VPS is suspended. Contact an admin to unsuspend.", False)
        if whitelisted:
            add_field(embed, "✅ Whitelisted", "This VPS is exempt from auto-suspension.", False)
        live_stats = f"**CPU Usage:** {stats['cpu']:.1f}%\n**Memory:** {stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)\n**Disk:** {stats['disk']}"
        add_field(embed, "📈 Live Usage", live_stats, False)
        pinggy_address = vps.get('pinggy_address')
        if pinggy_address:
            tunnel_host, tunnel_port = pinggy_address.split(":")
            add_field(embed, "🔌 SSH Tunnel", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
        else:
            add_field(embed, "🔌 SSH Tunnel", f"No active tunnel. Use 🔌 Reconnect Tunnel below.", False)
        add_field(embed, "🎮 Controls", "Use the buttons below to manage your VPS", False)
        return embed

    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            reinstall_button = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            reinstall_button.callback = lambda inter: self.action_callback(inter, 'reinstall')
            self.add_item(reinstall_button)
        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')
        stop_button = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')
        ssh_button = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary)
        ssh_button.callback = lambda inter: self.action_callback(inter, 'tmate')
        stats_button = discord.ui.Button(label="📊 Stats", style=discord.ButtonStyle.secondary)
        stats_button.callback = lambda inter: self.action_callback(inter, 'stats')
        restart_button = discord.ui.Button(label="🔁 Restart", style=discord.ButtonStyle.primary)
        restart_button.callback = lambda inter: self.action_callback(inter, 'restart')
        network_button = discord.ui.Button(label="🌐 Network", style=discord.ButtonStyle.primary)
        network_button.callback = lambda inter: self.action_callback(inter, 'network')
        backup_button = discord.ui.Button(label="📦 Snapshot Backup", style=discord.ButtonStyle.success)
        backup_button.callback = lambda inter: self.action_callback(inter, 'snapshot')
        sshx_button = discord.ui.Button(label="🖥️ SSHX Terminal Share", style=discord.ButtonStyle.primary)
        sshx_button.callback = lambda inter: self.action_callback(inter, 'sshx')
        health_button = discord.ui.Button(label="🩺 AI Health", style=discord.ButtonStyle.success)
        health_button.callback = lambda inter: self.action_callback(inter, 'health')
        ipv4_button = discord.ui.Button(label="🌍 SSH / IPv4 Connect", style=discord.ButtonStyle.primary)
        ipv4_button.callback = lambda inter: self.action_callback(inter, 'ipv4')
        refresh_button = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
        refresh_button.callback = lambda inter: self.action_callback(inter, 'refresh')
        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(restart_button)
        self.add_item(ssh_button)
        self.add_item(sshx_button)
        self.add_item(stats_button)
        self.add_item(network_button)
        self.add_item(ipv4_button)
        self.add_item(health_button)
        self.add_item(backup_button)
        self.add_item(refresh_button)
        if not self.is_shared:
            addport_button = discord.ui.Button(label="➕ Add Port", style=discord.ButtonStyle.primary)
            addport_button.callback = lambda inter: self.action_callback(inter, 'addport')
            self.add_item(addport_button)
            changepass_button = discord.ui.Button(label="🔐 Change Password", style=discord.ButtonStyle.danger)
            changepass_button.callback = lambda inter: self.action_callback(inter, 'changepass')
            self.add_item(changepass_button)
            reconnect_button = discord.ui.Button(label="🔌 Reconnect Tunnel", style=discord.ButtonStyle.primary)
            reconnect_button.callback = lambda inter: self.action_callback(inter, 'reconnect_tunnel')
            self.add_item(reconnect_button)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        await interaction.response.defer()
        new_embed = await self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.edit_original_response(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        if self.selected_index is None:
            await interaction.response.send_message(embed=create_error_embed("No VPS Selected", "Please select a VPS first."), ephemeral=True)
            return
        actual_idx = self.actual_index if self.is_shared else self.indices[self.selected_index]
        target_vps = vps_data[self.owner_id][actual_idx]
        suspended = target_vps.get('suspended', False)
        if suspended and not self.is_admin and action != 'stats':
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "This VPS is suspended. Contact an admin to unsuspend."), ephemeral=True)
            return
        container_name = target_vps["container_name"]
        node_id = target_vps['node_id']
        if action == 'stats':
            stats = await get_container_stats(container_name, node_id)
            stats_embed = create_info_embed("📈 Live Statistics", f"Real-time stats for `{container_name}`")
            add_field(stats_embed, "Status", f"`{stats['status'].upper()}`", True)
            add_field(stats_embed, "CPU", f"{stats['cpu']:.1f}%", True)
            add_field(stats_embed, "Memory", f"{stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)", True)
            add_field(stats_embed, "Disk", stats['disk'], True)
            add_field(stats_embed, "Uptime", stats['uptime'], True)
            await interaction.response.send_message(embed=stats_embed, ephemeral=True)
            return
        if action == 'refresh':
            await interaction.response.defer(ephemeral=True)
            new_embed = await self.create_vps_embed(self.selected_index)
            await interaction.edit_original_response(embed=new_embed, view=self)
            return
        if action == 'restart':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await execute_lxc(container_name, f"restart {container_name}", timeout=120, node_id=node_id)
                target_vps["status"] = "running"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("VPS Restarted", f"`{container_name}` restarted successfully."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Restart Failed", str(e)), ephemeral=True)
            return
        if action == 'network':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await execute_lxc(container_name, f"exec {container_name} -- bash -lc 'ip -brief addr; echo; ip route; echo; cat /etc/resolv.conf'", timeout=30, node_id=node_id)
                await interaction.followup.send(embed=create_info_embed("🌐 VPS Network", f"```text\\n{str(result)[:3500]}\\n```"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Network Check Failed", str(e)), ephemeral=True)
            return
        if action == 'ipv4':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                public_ip = get_public_ip(node_id)
                ssh_check = await execute_lxc(container_name, f"exec {container_name} -- bash -lc 'ss -lnt 2>/dev/null | grep -E \\\"(:22[[:space:]]|\\*:22)\\\" || systemctl is-active ssh sshd 2>/dev/null || true'", timeout=20, node_id=node_id)
                tunnel = target_vps.get('pinggy_address') or "No saved SSH tunnel"
                msg = f"**VPS:** `{container_name}`\\n**Node public IPv4:** `{public_ip or 'Not configured'}`\\n**SSH listener:**\\n```text\\n{str(ssh_check)[:1200]}\\n```\\n**Saved tunnel:** `{tunnel}`\\n\\nIf external IPv4 fails, verify provider firewall/NAT and open the assigned host port."
                await interaction.followup.send(embed=create_info_embed("🔗 SSH / IPv4 Diagnostics", msg), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("IPv4 Check Failed", str(e)), ephemeral=True)
            return
        if action == 'health':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                stats = await get_container_stats(container_name, node_id)
                checks = [f"Container status: {stats.get('status', 'unknown')}", f"CPU usage: {stats.get('cpu', 0):.1f}%", f"Memory: {stats.get('ram', {}).get('used', '?')}/{stats.get('ram', {}).get('total', '?')} MB", f"Disk: {stats.get('disk', 'Unknown')}", f"Node public IPv4: {get_public_ip(node_id) or 'Not configured'}", "SSH tunnel: " + ("saved" if target_vps.get('pinggy_address') else "not active")]
                await interaction.followup.send(embed=create_info_embed("🩺 VPS Health Check", "\\n".join(f"• {x}" for x in checks)), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Health Check Failed", str(e)), ephemeral=True)
            return
        if action == 'snapshot':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                snap_name = "zylex-auto-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                await execute_lxc(container_name, f"snapshot {container_name} {snap_name}", timeout=120, node_id=node_id)
                await interaction.followup.send(embed=create_success_embed("📦 Snapshot Backup Created", f"Snapshot `{snap_name}` created. This is a local LXD snapshot, not an off-site backup."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Snapshot Failed", str(e)), ephemeral=True)
            return
        if action == 'sshx':
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                # Only create a share link after the owner explicitly presses this button.
                # The link grants terminal access, so never post it in a public channel.
                safe_name = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", container_name)
                if not safe_name:
                    raise RuntimeError("Invalid container name; refusing to run a shell command.")
                script = (
                    "set +e; "
                    "if ! command -v sshx >/dev/null 2>&1; then "
                    "curl -fsSL --connect-timeout 10 --max-time 90 https://sshx.io/get | sh; "
                    "fi; "
                    "mkdir -p /tmp; "
                    "(nohup sshx > /tmp/zylex-sshx.log 2>&1 </dev/null &); "
                    "sleep 4; cat /tmp/zylex-sshx.log 2>/dev/null || true; "
                )
                output = await execute_lxc(
                    container_name,
                    f"exec {container_name} -- bash -lc {shlex.quote(script)}",
                    timeout=100, node_id=node_id
                )
                combined = str(output or "")
                # Some SSHX versions print the link after startup; if it is not in
                # captured output, read the short log from inside the guest.
                match = re.search(r"https://sshx\.io/s/[A-Za-z0-9_-]+(?:#[A-Za-z0-9_-]+)?", combined)
                if not match:
                    try:
                        log_text = await execute_lxc(
                            container_name,
                            f"exec {container_name} -- cat /tmp/zylex-sshx.log",
                            timeout=15, node_id=node_id
                        )
                        match = re.search(r"https://sshx\.io/s/[A-Za-z0-9_-]+(?:#[A-Za-z0-9_-]+)?", str(log_text or ""))
                    except Exception:
                        pass
                if not match:
                    raise RuntimeError("SSHX did not return a share URL. Check that the VPS has internet access and curl is installed, then retry.")
                link = match.group(0)
                embed = create_success_embed(
                    "🖥️ SSHX · Live Terminal Ready",
                    f"**VPS:** `{container_name}`\n**Private session link:** {link}\n\n⚠️ Anyone with this link may be able to access the shared terminal. Do not post it publicly."
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as exc:
                logger.warning("SSHX session start failed for %s: %s", container_name, exc)
                await interaction.followup.send(embed=create_error_embed("SSHX Start Failed", f"{str(exc)[:1200]}\nCheck VPS internet/DNS and retry."), ephemeral=True)
            return
        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall!"), ephemeral=True)
                return
            if suspended:
                await interaction.response.send_message(embed=create_error_embed("Cannot Reinstall", "Unsuspend the VPS first."), ephemeral=True)
                return
            ram_gb = int(target_vps['ram'].replace('GB', ''))
            cpu = int(target_vps['cpu'])
            storage_gb = int(target_vps['storage'].replace('GB', ''))
            confirm_embed = create_warning_embed("Reinstall Warning",
                f"⚠️ **WARNING:** This will erase all data on VPS `{container_name}` and reinstall a fresh OS.\n\n"
                f"This action cannot be undone. Continue?")
            class ConfirmView(discord.ui.View):
                def __init__(self, parent_view, container_name, owner_id, actual_idx, ram_gb, cpu, storage_gb, node_id):
                    super().__init__(timeout=60)
                    self.parent_view = parent_view
                    self.container_name = container_name
                    self.owner_id = owner_id
                    self.actual_idx = actual_idx
                    self.ram_gb = ram_gb
                    self.cpu = cpu
                    self.storage_gb = storage_gb
                    self.node_id = node_id

                @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
                async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
                    await inter.response.defer(ephemeral=True)
                    try:
                        await inter.followup.send(embed=create_info_embed("Deleting Container", f"Forcefully removing container `{self.container_name}`..."), ephemeral=True)
                        await execute_lxc(self.container_name, f"delete {self.container_name} --force", node_id=self.node_id)
                        os_view = ReinstallOSSelectView(self.parent_view, self.container_name, self.owner_id, self.actual_idx, self.ram_gb, self.cpu, self.storage_gb, self.node_id)
                        await inter.followup.send(embed=create_info_embed("Select OS", "Choose the new OS for reinstallation."), view=os_view, ephemeral=True)
                    except Exception as e:
                        await inter.followup.send(embed=create_error_embed("Delete Failed", f"Error: {str(e)}"), ephemeral=True)

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
                    new_embed = await self.parent_view.create_vps_embed(self.parent_view.selected_index)
                    await inter.response.edit_message(embed=new_embed, view=self.parent_view)

            await interaction.response.send_message(embed=confirm_embed, view=ConfirmView(self, container_name, self.owner_id, actual_idx, ram_gb, cpu, storage_gb, node_id), ephemeral=True)
            return
        if action == 'addport':
            owner_id = self.owner_id

            class AddPortModal(discord.ui.Modal, title="Add Port Forward"):
                vps_port_input = discord.ui.TextInput(label="VPS Port (1-65535)", placeholder="e.g. 8080", max_length=5)

                async def on_submit(self, modal_inter: discord.Interaction):
                    await modal_inter.response.defer(ephemeral=True)
                    try:
                        vps_port = int(self.vps_port_input.value)
                        if vps_port < 1 or vps_port > 65535:
                            raise ValueError
                    except ValueError:
                        await modal_inter.followup.send(embed=create_error_embed("Invalid Port", "Port must be a number between 1 and 65535."), ephemeral=True)
                        return
                    allocated = get_user_allocation(owner_id)
                    used = get_user_used_ports(owner_id)
                    if used >= allocated:
                        await modal_inter.followup.send(embed=create_error_embed("Quota Exceeded", f"No available port slots. Allocated: {allocated}, Used: {used}. Contact an admin for more."), ephemeral=True)
                        return
                    host_port = await create_port_forward(owner_id, container_name, vps_port, node_id)
                    if host_port:
                        public_ip = get_public_ip(node_id)
                        success_embed = create_success_embed("Port Forward Created", f"VPS port `{vps_port}` forwarded to host port `{host_port}` (TCP & UDP).")
                        if public_ip:
                            add_field(success_embed, "IPv4 Access", f"`{public_ip}:{host_port}` → VPS:{vps_port}", False)
                        else:
                            add_field(success_embed, "IPv4 setup required", f"Forward created on node port `{host_port}`, but its public IPv4 is not configured. Set `NODE_PUBLIC_IPS` (JSON keyed by node ID/name) or `YOUR_SERVER_IP`; also open the port in the host/cloud firewall.", False)
                        await modal_inter.followup.send(embed=success_embed, ephemeral=True)
                        try:
                            dm_user = await bot.fetch_user(int(owner_id))
                            dm_embed = create_success_embed("🔌 Port Forward Created", f"A new port forward was created for your VPS `{container_name}`.")
                            add_field(dm_embed, "Details", f"**VPS Port:** {vps_port}\n**External Port:** {host_port}\n**Access:** `{public_ip}:{host_port}` (TCP & UDP)" if public_ip else f"**VPS Port:** {vps_port}\n**External Port:** {host_port}\nPublic IPv4 not configured; ask an admin to configure the node IP and firewall.", False)
                            await dm_user.send(embed=dm_embed)
                        except discord.Forbidden:
                            pass
                    else:
                        await modal_inter.followup.send(embed=create_error_embed("Failed", "Could not assign a host port. Try again later."), ephemeral=True)

            await interaction.response.send_modal(AddPortModal())
            return
        await interaction.response.defer(ephemeral=True)
        suspended = target_vps.get('suspended', False)
        if suspended:
            target_vps['suspended'] = False
            save_vps_data()
        if action == 'start':
            try:
                await safe_start_container(container_name, node_id)
                target_vps["status"] = "running"
                save_vps_data()
                await apply_internal_permissions(container_name, node_id)
                readded = await recreate_port_forwards(container_name)
                await interaction.followup.send(embed=create_success_embed("VPS Started", f"VPS `{container_name}` is now running! Re-added {readded} port forwards."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Start Failed", str(e)), ephemeral=True)
        elif action == 'stop':
            try:
                await execute_lxc(container_name, f"stop {container_name}", timeout=120, node_id=node_id)
                target_vps["status"] = "stopped"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("VPS Stopped", f"VPS `{container_name}` has been stopped!"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)
        elif action == 'tmate':
            if suspended:
                await interaction.followup.send(embed=create_error_embed("Access Denied", "Cannot access suspended VPS."), ephemeral=True)
                return
            await interaction.followup.send(embed=create_info_embed("SSH Access", "Generating SSH connection..."), ephemeral=True)
            try:
                # Check if tmate is installed
                try:
                    await execute_lxc(container_name, f"exec {container_name} -- which tmate", node_id=node_id)
                except:
                    await interaction.followup.send(embed=create_info_embed("Installing SSH", "Installing tmate..."), ephemeral=True)
                    await execute_lxc(container_name, f"exec {container_name} -- apt-get update -y", node_id=node_id)
                    await execute_lxc(container_name, f"exec {container_name} -- apt-get install tmate -y", node_id=node_id)
                    await interaction.followup.send(embed=create_success_embed("Installed", "SSH service installed!"), ephemeral=True)
                session_name = f"{BOT_NAME.lower()}-session-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                await execute_lxc(container_name, f"exec {container_name} -- tmate -S /tmp/{session_name}.sock new-session -d", node_id=node_id)
                await asyncio.sleep(3)
                ssh_output = await execute_lxc(container_name, f"exec {container_name} -- tmate -S /tmp/{session_name}.sock display -p '#{{tmate_ssh}}'", node_id=node_id)
                ssh_url = ssh_output.strip()
                if ssh_url:
                    try:
                        ssh_embed = create_embed("🔑 SSH Access", f"SSH connection for VPS `{container_name}`:", 0x00ff88)
                        add_field(ssh_embed, "Command", f"```{ssh_url}```", False)
                        add_field(ssh_embed, "⚠️ Security", "This link is temporary. Do not share it.", False)
                        add_field(ssh_embed, "📝 Session", f"Session ID: {session_name}", False)
                        await interaction.user.send(embed=ssh_embed)
                        await interaction.followup.send(embed=create_success_embed("SSH Sent", f"Check your DMs for SSH link! Session: {session_name}"), ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(embed=create_error_embed("DM Failed", "Enable DMs to receive SSH link!"), ephemeral=True)
                else:
                    await interaction.followup.send(embed=create_error_embed("SSH Failed", "No SSH URL generated."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("SSH Error", str(e)), ephemeral=True)
        elif action == 'changepass':
            try:
                new_password = generate_password()
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"echo 'root:{new_password}' | chpasswd\"", node_id=node_id, timeout=60)
                target_vps['root_password'] = new_password
                save_vps_data()
                try:
                    dm_user = await bot.fetch_user(int(self.owner_id))
                    dm_embed = create_success_embed("🔐 Root Password Changed", f"Your root password for VPS `{container_name}` has been changed.")
                    add_field(dm_embed, "New Password", f"`{new_password}`", False)
                    add_field(dm_embed, "⚠️ Security", "Keep this password private. Do not share it.", False)
                    await dm_user.send(embed=dm_embed)
                    await interaction.followup.send(embed=create_success_embed("Password Changed", "Check your DMs for the new password!"), ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(embed=create_success_embed("Password Changed", f"New password: `{new_password}`\n(Enable DMs to receive future passwords privately)"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Password Change Failed", str(e)), ephemeral=True)
        elif action == 'reconnect_tunnel':
            try:
                await interaction.followup.send(embed=create_info_embed("Reconnecting", "Setting up a fresh SSH tunnel, this can take up to 30 seconds..."), ephemeral=True)
                new_address = await establish_pinggy_tunnel(container_name, node_id)
                target_vps['pinggy_address'] = new_address
                save_vps_data()
                if new_address:
                    tunnel_host, tunnel_port = new_address.split(":")
                    try:
                        dm_user = await bot.fetch_user(int(self.owner_id))
                        dm_embed = create_success_embed("🔌 SSH Tunnel Reconnected", f"A fresh SSH tunnel for VPS `{container_name}` is ready.")
                        add_field(dm_embed, "🔑 SSH Login", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n**User:** `root`\n\n```ssh root@{tunnel_host} -p {tunnel_port}```", False)
                        await dm_user.send(embed=dm_embed)
                        await interaction.followup.send(embed=create_success_embed("Tunnel Reconnected", "Check your DMs for the new address!"), ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(embed=create_success_embed("Tunnel Reconnected", f"**Host:** `{tunnel_host}`\n**Port:** `{tunnel_port}`\n(Enable DMs to receive future addresses privately)"), ephemeral=True)
                else:
                    await interaction.followup.send(embed=create_error_embed("Reconnect Failed", "Could not establish a tunnel. Make sure the VPS is running and try again."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Reconnect Failed", str(e)), ephemeral=True)
        new_embed = await self.create_vps_embed(self.selected_index)
        await interaction.edit_original_response(embed=new_embed, view=self)

@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    if user:
        if str(ctx.author.id) not in main_admin_ids and str(ctx.author.id) not in admin_data.get("admins", []):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        user_id = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any {BOT_NAME} VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS", f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = create_error_embed("No VPS Found", f"You don't have any {BOT_NAME} VPS. Contact an admin to create one.")
            add_field(embed, "Quick Actions", f"• `{PREFIX}manage` - Manage VPS\n• Contact admin for VPS creation", False)
            await ctx.send(embed=embed)
            return
        view = ManageView(user_id, vps_list)
        embed = await view.get_initial_embed()
        await ctx.send(embed=embed, view=view)

async def get_node_status(node_id: int) -> str:
    node = get_node(node_id)
    if not node:
        return "❓ Unknown"
    if node['is_local']:
        return "🟢 Online (Local)"
    try:
        response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
        if response.status_code == 200:
            return "🟢 Online"
        else:
            return "🔴 Offline"
    except Exception as e:
        logger.error(f"Failed to ping node {node['name']}: {e}")
        return "🔴 Offline"


def get_host_disk_usage():
    try:
        result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            total = parts[1]
            used = parts[2]
            perc = parts[4]
            return f"{used}/{total} ({perc})"
        return "Unknown"
    except Exception as e:
        logger.error(f"Error getting disk usage: {e}")
        return "Unknown"


async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {
            "cpu": get_host_cpu_usage(),
            "ram": get_host_ram_usage(),
            "disk": get_host_disk_usage()
        }
    else:
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            stats = response.json()
            # Fallbacks if remote API doesn't provide
            stats['disk'] = stats.get('disk', 'Unknown')
            return stats
        except Exception as e:
            logger.error(f"Failed to get host stats from node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown"}


@bot.command(name='vps-list')
@is_admin()
async def vps_list(ctx, node_id: int = 1):
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID {node_id} not found."))
        return

    # Get node status
    status = await get_node_status(node_id)
    is_online = status.startswith("🟢")

    # Get node resource stats (will use defaults if offline)
    stats = await get_host_stats(node_id)
    cpu_usage = stats.get('cpu', 0.0)
    ram_usage = stats.get('ram', 0.0)
    disk_usage = stats.get('disk', 'Unknown')

    # Resources field text (modern: compact inline stats with progress-like emojis)
    if is_online:
        resources_text = (
            f"**CPU** {cpu_usage:.0f}% {'█' * int(cpu_usage / 5) + '░' * (20 - int(cpu_usage / 5))} "
            f"\n**RAM** {ram_usage:.0f}% {'█' * int(ram_usage / 5) + '░' * (20 - int(ram_usage / 5))} "
            f"\n**Disk** {disk_usage}"
        )
    else:
        resources_text = "⚠️ Resources unavailable (Offline)"

    # Get VPS capacity
    current_vps = get_current_vps_count(node_id)
    total_capacity = node['total_vps']
    capacity_percent = (current_vps / total_capacity * 100) if total_capacity > 0 else 0
    capacity_text = f"{current_vps}/{total_capacity} ({capacity_percent:.0f}%)"

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps WHERE node_id = ?', (node_id,))
    rows = cur.fetchall()
    conn.close()

    total_vps = len(rows)

    # Modern counters: use more intuitive emojis and clean layout
    running = 0
    stopped = 0
    suspended = 0
    other = 0
    vps_info = []
    for i, row in enumerate(rows, 1):
        vps = dict(row)
        user_id = vps['user_id']
        try:
            user = await bot.fetch_user(int(user_id))
            username = user.name
        except:
            username = f"Unknown ({user_id})"

        status = vps.get('status', 'unknown')
        suspended_flag = vps.get('suspended', False)

        # Count logic: suspended first, then status if not suspended
        if suspended_flag:
            suspended += 1
        elif status == 'running':
            running += 1
        elif status == 'stopped':
            stopped += 1
        else:
            other += 1

        # Modern emoji: vibrant and status-specific
        status_emoji = "🟢" if status == 'running' and not suspended_flag else "🟡" if suspended_flag else "🔴"
        vps_status = status.upper()
        if suspended_flag:
            vps_status += " (SUSPENDED)"
        if vps.get('whitelisted', False):
            vps_status += " (WHITELISTED)"
        config = vps.get('config', 'Custom')
        vps_info.append(f"{status_emoji} **{i}.** {username} • `{vps['container_name']}`\n _{vps_status} | {config}_")

    # Create main embed (modern: gradient-inspired colors, clean typography)
    color = 0x10b981 if is_online else 0xef4444  # Teal green / Soft red for modern feel
    embed = create_embed(
        title=f"🖥️ VPS Dashboard - {node['name']}",
        description=f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
        color=color
    )
    embed.set_thumbnail(url=node.get('thumbnail_url', None))

    # Inline status and capacity for compact top row
    add_field(embed, "📡 **Status**", status, True)
    add_field(embed, "🗄️ **Capacity**", capacity_text, True)

    # Resources field with modern bar visualization
    add_field(embed, "📊 **Resources**", resources_text, False)

    # Summary field (modern: compact bullet-like with inline emojis)
    summary_text = (
        f"**Total:** {total_vps} 📊\n"
        f"**Running:** {running} 🟢\n"
        f"**Stopped:** {stopped} ⏸️\n"
        f"**Suspended:** {suspended} 🟡"
    )
    if other > 0:
        summary_text += f"\n**Other:** {other} ⚠️"
    add_field(embed, "📈 **Summary**", summary_text, True)

    # VPS List - chunked embeds with modern pagination
    if vps_info:
        chunk_size = 6  # Smaller chunks for cleaner mobile-friendly embeds
        chunks = [vps_info[i:i + chunk_size] for i in range(0, len(vps_info), chunk_size)]
        first_chunk_text = "\n".join(chunks[0])
        add_field(embed, "📋 **Active VPS (1/{len(chunks)})**", f"```{first_chunk_text}```", False)

        # Paginated follow-ups with consistent styling
        for idx, chunk in enumerate(chunks[1:], 2):
            page_embed = create_embed(
                title=f"🖥️ VPS Dashboard - {node['name']} (Page {idx}/{len(chunks)})",
                description=f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
                color=color
            )
            chunk_text = "\n".join(chunk)
            add_field(page_embed, "📋 **VPS List**", f"```{chunk_text}```", False)
            page_embed.set_footer(text=f"Total: {total_vps} VPS | Powered by Your Bot")
            await ctx.send(embed=page_embed)
    else:
        add_field(embed, "📋 **VPS List**", "No deployments yet. Launch one! 🚀", False)

    embed.set_footer(text=f"Refresh with !vps-list {node_id} | {len(vps_info)} shown")
    await ctx.send(embed=embed)

@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    total_vps = 0
    total_users = len(vps_data)
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    whitelisted_vps = 0
    vps_info = []
    user_summary = []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            user_vps_count = len(vps_list)
            user_running = sum(1 for vps in vps_list if vps.get('status') == 'running' and not vps.get('suspended', False))
            user_stopped = sum(1 for vps in vps_list if vps.get('status') == 'stopped')
            user_suspended = sum(1 for vps in vps_list if vps.get('suspended', False))
            user_whitelisted = sum(1 for vps in vps_list if vps.get('whitelisted', False))
            total_vps += user_vps_count
            running_vps += user_running
            stopped_vps += user_stopped
            suspended_vps += user_suspended
            whitelisted_vps += user_whitelisted
            user_summary.append(f"**{user.name}** ({user.mention}) - {user_vps_count} VPS ({user_running} running, {user_suspended} suspended, {user_whitelisted} whitelisted)")
            for i, vps in enumerate(vps_list):
                node = get_node(vps['node_id'])
                node_name = node['name'] if node else "Unknown"
                status_emoji = "🟢" if vps.get('status') == 'running' and not vps.get('suspended', False) else "🟡" if vps.get('suspended', False) else "🔴"
                status_text = vps.get('status', 'unknown').upper()
                if vps.get('suspended', False):
                    status_text += " (SUSPENDED)"
                if vps.get('whitelisted', False):
                    status_text += " (WHITELISTED)"
                vps_info.append(f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` - {vps.get('config', 'Custom')} - {status_text} (Node: {node_name})")
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")
    embed = create_embed("All VPS Information", "Complete overview of all VPS deployments and user statistics", 0x1a1a1a)
    add_field(embed, "System Overview", f"**Total Users:** {total_users}\n**Total VPS:** {total_vps}\n**Running:** {running_vps}\n**Stopped:** {stopped_vps}\n**Suspended:** {suspended_vps}\n**Whitelisted:** {whitelisted_vps}", False)
    await ctx.send(embed=embed)
    if user_summary:
        embed = create_embed("User Summary", f"Summary of all users and their VPS", 0x1a1a1a)
        summary_text = "\n".join(user_summary)
        chunks = [summary_text[i:i+1024] for i in range(0, len(summary_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            add_field(embed, f"Users (Part {idx})", chunk, False)
        await ctx.send(embed=embed)
    if vps_info:
        vps_text = "\n".join(vps_info)
        chunks = [vps_text[i:i+1024] for i in range(0, len(vps_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"VPS Details (Part {idx})", "List of all VPS deployments", 0x1a1a1a)
            add_field(embed, "VPS List", chunk, False)
            await ctx.send(embed=embed)

@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    owner_id = str(owner.id)
    user_id = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or owner doesn't have a VPS."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id, actual_index=vps_number - 1)
    embed = await view.get_initial_embed()
    await ctx.send(embed=embed, view=view)

@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access to this VPS!"))
        return
    vps["shared_with"].append(shared_user_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Granted", f"You have access to VPS #{vps_number} from {ctx.author.mention}. Use `{PREFIX}manage-shared {ctx.author.mention} {vps_number}`", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access to this VPS!"))
        return
    vps["shared_with"].remove(shared_user_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("Access Revoked", f"Access to VPS #{vps_number} revoked from {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Revoked", f"Your access to VPS #{vps_number} by {ctx.author.mention} has been revoked.", 0xff3366))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='ports-add-user')
@is_admin()
async def ports_add_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    user_id = str(user.id)
    allocate_ports(user_id, amount)
    embed = create_success_embed("Ports Allocated", f"Allocated {amount} port slots to {user.mention}.")
    add_field(embed, "Quota", f"Total: {get_user_allocation(user_id)} slots", False)
    await ctx.send(embed=embed)
    try:
        dm_embed = create_info_embed("Port Slots Allocated", f"You have been granted {amount} additional port forwarding slots by an admin.\nUse `{PREFIX}ports list` to view your quota and active forwards.")
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-remove-user')
@is_admin()
async def ports_remove_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    user_id = str(user.id)
    current = get_user_allocation(user_id)
    if amount > current:
        amount = current
    deallocate_ports(user_id, amount)
    remaining = get_user_allocation(user_id)
    embed = create_success_embed("Ports Deallocated", f"Removed {amount} port slots from {user.mention}.")
    add_field(embed, "Remaining Quota", f"{remaining} slots", False)
    await ctx.send(embed=embed)
    try:
        dm_embed = create_warning_embed("Port Slots Reduced", f"Your port forwarding quota has been reduced by {amount} slots by an admin.\nRemaining: {remaining} slots.")
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-revoke')
@is_admin()
async def ports_revoke(ctx, forward_id: int):
    success, user_id = await remove_port_forward(forward_id, is_admin=True)
    if success and user_id:
        try:
            user = await bot.fetch_user(int(user_id))
            dm_embed = create_warning_embed("Port Forward Revoked", f"One of your port forwards (ID: {forward_id}) has been revoked by an admin.")
            await user.send(embed=dm_embed)
        except:
            pass
        await ctx.send(embed=create_success_embed("Revoked", f"Port forward ID {forward_id} revoked."))
    else:
        await ctx.send(embed=create_error_embed("Failed", "Port forward ID not found or removal failed."))

@bot.command(name='ports')
async def ports_command(ctx, subcmd: str = None, *args):
    user_id = str(ctx.author.id)
    allocated = get_user_allocation(user_id)
    used = get_user_used_ports(user_id)
    available = allocated - used
    if subcmd is None:
        embed = create_info_embed("Port Forwarding Help", f"**Your Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        add_field(embed, "Commands", f"{PREFIX}ports add <vps_num> <port>\n{PREFIX}ports list\n{PREFIX}ports remove <id>", False)
        await ctx.send(embed=embed)
        return
    if subcmd == 'add':
        if len(args) < 2:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports add <vps_number> <vps_port>"))
            return
        try:
            vps_num = int(args[0])
            vps_port = int(args[1])
            if vps_port < 1 or vps_port > 65535:
                raise ValueError
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "VPS number and port must be positive integers (port: 1-65535)."))
            return
        vps_list = vps_data.get(user_id, [])
        if vps_num < 1 or vps_num > len(vps_list):
            await ctx.send(embed=create_error_embed("Invalid VPS", f"Invalid VPS number (1-{len(vps_list)}). Use {PREFIX}myvps to list."))
            return
        vps = vps_list[vps_num - 1]
        container = vps['container_name']
        node_id = vps['node_id']
        if used >= allocated:
            await ctx.send(embed=create_error_embed("Quota Exceeded", f"No available slots. Allocated: {allocated}, Used: {used}. Contact admin for more."))
            return
        host_port = await create_port_forward(user_id, container, vps_port, node_id)
        if host_port:
            embed = create_success_embed("Port Forward Created", f"VPS #{vps_num} port {vps_port} (TCP/UDP) forwarded to host port {host_port}.")
            public_ip = get_public_ip(node_id)
            if public_ip:
                add_field(embed, "IPv4 Access", f"External: {public_ip}:{host_port} → VPS:{vps_port} (TCP & UDP)", False)
            else:
                add_field(embed, "IPv4 setup required", f"Port `{host_port}` was forwarded, but the public IPv4 is not configured. Set `NODE_PUBLIC_IPS` or `YOUR_SERVER_IP` and allow the port in the node firewall.", False)
            add_field(embed, "Quota Update", f"Used: {used + 1}/{allocated}", False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Failed", "Could not assign host port. Try again later."))
    elif subcmd == 'list':
        forwards = get_user_forwards(user_id)
        embed = create_info_embed("Your Port Forwards", f"**Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        if not forwards:
            add_field(embed, "Forwards", "No active port forwards.", False)
        else:
            text = []
            for f in forwards:
                vps_num = next((i+1 for i, v in enumerate(vps_data.get(user_id, [])) if v['container_name'] == f['vps_container']), 'Unknown')
                created = datetime.fromisoformat(f['created_at']).strftime('%Y-%m-%d %H:%M')
                text.append(f"**ID {f['id']}** - VPS #{vps_num}: {f['vps_port']} (TCP/UDP) → {f['host_port']} (Created: {created})")
            add_field(embed, "Active Forwards", "\n".join(text[:10]), False)
            if len(forwards) > 10:
                add_field(embed, "Note", f"Showing 10 of {len(forwards)}. Remove unused with {PREFIX}ports remove <id>.")
        await ctx.send(embed=embed)
    elif subcmd == 'remove':
        if len(args) < 1:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports remove <forward_id>"))
            return
        try:
            fid = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Forward ID must be an integer."))
            return
        success, _ = await remove_port_forward(fid)
        if success:
            embed = create_success_embed("Removed", f"Port forward {fid} removed (TCP & UDP).")
            add_field(embed, "Quota Update", f"Used: {used - 1}/{allocated}", False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Not Found", "Forward ID not found. Use !ports list."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Subcommand", f"Use: add <vps_num> <port>, list, remove <id>"))

@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    user_id = str(user.id)

    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed(
            "Invalid VPS",
            "Invalid VPS number or user doesn't have that VPS."
        ))
        return

    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    node_id = vps.get("node_id", 1)

    await ctx.send(embed=create_info_embed(
        "Deleting VPS",
        f"Removing VPS #{vps_number} for {user.mention}..."
    ))

    node_result = "Not checked"

    # 1️⃣ Try deleting container
    try:
        await execute_lxc(container_name, f"delete {container_name} --force", node_id=node_id)
        node_result = "Container deleted successfully."
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["not found", "does not exist", "no such container"]):
            node_result = "Container not found (force DB cleanup)."
        else:
            node_result = f"Container delete failed: {e}"

    # 2️⃣ DELETE FROM DATABASE (THIS IS THE FIX)
    conn = get_db()
    cur = conn.cursor()

    cur.execute("DELETE FROM vps WHERE container_name = ?", (container_name,))
    cur.execute("DELETE FROM port_forwards WHERE vps_container = ?", (container_name,))

    conn.commit()
    conn.close()

    # 3️⃣ Remove from memory
    del vps_data[user_id][vps_number - 1]
    if not vps_data[user_id]:
        del vps_data[user_id]

        # Remove VPS role if needed
        if ctx.guild:
            role = await get_or_create_vps_role(ctx.guild)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason="No VPS ownership")
                except discord.Forbidden:
                    logger.warning(f"Failed to remove VPS role from {user.name}")

    save_vps_data()

    # 4️⃣ Success embed
    embed = create_success_embed("🌟 UnixNodes - VPS Deleted Successfully")
    add_field(embed, "Owner", user.mention, True)
    add_field(embed, "VPS Number", f"#{vps_number}", True)
    add_field(embed, "Container", container_name, False)
    add_field(embed, "Node Result", node_result, False)
    add_field(embed, "Reason", reason, False)

    await ctx.send(embed=embed)

def _find_vps_by_identifier(vps_identifier: str):
    """Resolve VPS by database ID or exact container name."""
    identifier = str(vps_identifier).strip()
    for owner_id, entries in vps_data.items():
        for index, item in enumerate(entries):
            if str(item.get('id', '')) == identifier or item.get('container_name') == identifier:
                return str(owner_id), index, item
    return None, None, None


def _parse_expiry(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone() if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


@bot.command(name='set-expiration')
@is_admin()
async def set_vps_expiration(ctx, vps_id: str, days: int):
    if days < 1 or days > 3650:
        await ctx.send(embed=create_error_embed('Invalid Duration', 'Days must be between 1 and 3650.'))
        return
    _, _, vps = _find_vps_by_identifier(vps_id)
    if not vps:
        await ctx.send(embed=create_error_embed('VPS Not Found', f'No VPS matches `{vps_id}`. Use the database VPS ID or exact container name.'))
        return
    from datetime import timedelta
    expiry = datetime.now().astimezone() + timedelta(days=days)
    vps['expires_at'] = expiry.isoformat()
    save_vps_data()
    await ctx.send(embed=create_success_embed('VPS Expiration Set', f"VPS `{vps['container_name']}` (DB ID `{vps.get('id', '?')}`) expires **{expiry.strftime('%Y-%m-%d %H:%M %Z')}** ({days} days from now)."))


@bot.command(name='renew-vps')
@is_admin()
async def renew_vps_expiration(ctx, vps_id: str, days: int = 30):
    if days < 1 or days > 3650:
        await ctx.send(embed=create_error_embed('Invalid Duration', 'Days must be between 1 and 3650.'))
        return
    _, _, vps = _find_vps_by_identifier(vps_id)
    if not vps:
        await ctx.send(embed=create_error_embed('VPS Not Found', f'No VPS matches `{vps_id}`. Use the database VPS ID or exact container name.'))
        return
    from datetime import timedelta
    now = datetime.now().astimezone()
    current = _parse_expiry(vps.get('expires_at'))
    expiry = (current if current and current > now else now) + timedelta(days=days)
    vps['expires_at'] = expiry.isoformat()
    save_vps_data()
    await ctx.send(embed=create_success_embed('VPS Renewed', f"VPS `{vps['container_name']}` renewed by **{days} days**. New expiry: **{expiry.strftime('%Y-%m-%d %H:%M %Z')}**."))


@bot.command(name='vps-expiration')
@is_admin()
async def vps_expiration(ctx, vps_id: str = None):
    rows = []
    for owner_id, entries in vps_data.items():
        for item in entries:
            if vps_id and str(item.get('id', '')) != str(vps_id) and item.get('container_name') != vps_id:
                continue
            expiry = _parse_expiry(item.get('expires_at'))
            if not expiry:
                state = 'No expiry set'
            else:
                now = datetime.now().astimezone()
                state = ('EXPIRED' if expiry <= now else f'{(expiry-now).days} day(s) remaining') + f" — {expiry.strftime('%Y-%m-%d %H:%M %Z')}"
            rows.append(f"**{item.get('container_name','unknown')}** (ID `{item.get('id','?')}`) · Owner <@{owner_id}>\n{state}")
    if not rows:
        await ctx.send(embed=create_error_embed('No VPS Found', f'No VPS matches `{vps_id}`.' if vps_id else 'No VPS records are available.'))
        return
    embed = create_info_embed('VPS Expiration Status', '\n\n'.join(rows[:20]))
    if len(rows) > 20:
        add_field(embed, 'More Results', f'Showing 20 of {len(rows)} VPS records.')
    await ctx.send(embed=embed)


@bot.command(name='add-resources')
@is_admin()
async def add_resources(ctx, vps_id: str, ram: int = None, cpu: int = None, disk: int = None):
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Please specify at least one resource to add (ram, cpu, or disk)"))
        return
    found_vps = None
    user_id = None
    vps_index = None
    for uid, vps_list in vps_data.items():
        for i, vps in enumerate(vps_list):
            if vps['container_name'] == vps_id:
                found_vps = vps
                user_id = uid
                vps_index = i
                break
        if found_vps:
            break
    if not found_vps:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with ID: `{vps_id}`"))
        return
    node_id = found_vps['node_id']
    was_running = found_vps.get('status') == 'running' and not found_vps.get('suspended', False)
    disk_changed = disk is not None
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping VPS", f"Stopping VPS `{vps_id}` to apply resource changes..."))
        try:
            await execute_lxc(vps_id, "stop {vps_id}", node_id=node_id)
            found_vps['status'] = 'stopped'
            save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", f"Error stopping VPS: {str(e)}"))
            return
    changes = []
    try:
        current_ram_gb = int(found_vps['ram'].replace('GB', ''))
        current_cpu = int(found_vps['cpu'])
        current_disk_gb = int(found_vps['storage'].replace('GB', ''))
        new_ram_gb = current_ram_gb
        new_cpu = current_cpu
        new_disk_gb = current_disk_gb
        if ram is not None and ram > 0:
            new_ram_gb += ram
            ram_mb = new_ram_gb * 1024
            await execute_lxc(vps_id, f"config set {vps_id} limits.memory {ram_mb}MB", node_id=node_id)
            changes.append(f"RAM: +{ram}GB (New total: {new_ram_gb}GB)")
        if cpu is not None and cpu > 0:
            new_cpu += cpu
            await execute_lxc(vps_id, f"config set {vps_id} limits.cpu {new_cpu}", node_id=node_id)
            changes.append(f"CPU: +{cpu} cores (New total: {new_cpu} cores)")
        if disk is not None and disk > 0:
            new_disk_gb += disk
            await execute_lxc(vps_id, f"config device set {vps_id} root size={new_disk_gb}GB", node_id=node_id)
            changes.append(f"Disk: +{disk}GB (New total: {new_disk_gb}GB)")
        found_vps['ram'] = f"{new_ram_gb}GB"
        found_vps['cpu'] = str(new_cpu)
        found_vps['storage'] = f"{new_disk_gb}GB"
        found_vps['config'] = f"{new_ram_gb}GB RAM / {new_cpu} CPU / {new_disk_gb}GB Disk"
        vps_data[user_id][vps_index] = found_vps
        save_vps_data()
        if was_running:
            await execute_lxc(vps_id, f"start {vps_id}", node_id=node_id)
            found_vps['status'] = 'running'
            save_vps_data()
            await apply_internal_permissions(vps_id, node_id)
            await recreate_port_forwards(vps_id)
        embed = create_success_embed("Resources Added", f"Successfully added resources to VPS `{vps_id}`")
        add_field(embed, "Changes Applied", "\n".join(changes), False)
        if disk_changed:
            add_field(embed, "Disk Note", "Run `sudo resize2fs /` inside the VPS to expand the filesystem.", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Resource Addition Failed", f"Error: {str(e)}"))


@bot.command(name='status')
@is_admin()
async def system_status(ctx):
    """
    Show complete system status including:
    - Bot uptime
    - Total nodes & their status
    - Running/stopped nodes count
    - Total RAM/CPU/DISK allocated vs free
    - Total VPS & users
    - Running/stopped/suspended VPS counts
    - Total admin users
    - Whitelisted VPS
    """
    
    # Start timing for response time
    start_time = time.time()
    
    # Get bot uptime
    bot_start_time = datetime.now() - datetime.fromtimestamp(start_time - bot.latency)
    bot_uptime = str(bot_start_time).split('.')[0]  # Remove microseconds
    
    # Get total nodes
    nodes = get_nodes()
    total_nodes = len(nodes)
    
    # Node status counters
    running_nodes = 0
    stopped_nodes = 0
    local_nodes = 0
    remote_nodes = 0
    
    # Node resource tracking
    total_node_cpu_allocated = 0
    total_node_ram_allocated = 0
    total_node_disk_allocated = 0
    total_node_cpu_free = 0
    total_node_ram_free = 0
    total_node_disk_free = 0
    
    # VPS counters
    total_vps = 0
    total_users = len(vps_data)
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    whitelisted_vps = 0
    
    # Admin counters
    total_admins = len(admin_data.get("admins", [])) + 1  # +1 for main admin
    
    # Port statistics
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()
    
    # Resource counters for all VPS
    total_ram_allocated = 0
    total_cpu_allocated = 0
    total_disk_allocated = 0
    
    # Process all VPS data
    for user_id, vps_list in vps_data.items():
        total_vps += len(vps_list)
        
        for vps in vps_list:
            # Count status
            if vps.get('suspended', False):
                suspended_vps += 1
            elif vps.get('status') == 'running':
                running_vps += 1
            else:
                stopped_vps += 1
            
            # Count whitelisted
            if vps.get('whitelisted', False):
                whitelisted_vps += 1
            
            # Calculate allocated resources
            try:
                ram_gb = int(vps['ram'].replace('GB', ''))
                total_ram_allocated += ram_gb
            except:
                pass
            
            try:
                cpu_cores = int(vps['cpu'])
                total_cpu_allocated += cpu_cores
            except:
                pass
            
            try:
                disk_gb = int(vps['storage'].replace('GB', ''))
                total_disk_allocated += disk_gb
            except:
                pass
    
    # Check node status and calculate free resources
    node_statuses = []
    
    for node in nodes:
        # Determine node type
        if node['is_local']:
            local_nodes += 1
            node_type = "🖥️ Local"
        else:
            remote_nodes += 1
            node_type = "🌐 Remote"
        
        # Check node status
        if node['is_local']:
            status = "🟢 Online"
            running_nodes += 1
            
            # Get local resources (approximate)
            try:
                # Get system memory
                mem_result = subprocess.run(['free', '-m'], capture_output=True, text=True)
                mem_lines = mem_result.stdout.splitlines()
                if len(mem_lines) > 1:
                    mem = mem_lines[1].split()
                    total_ram_mb = int(mem[1])
                    used_ram_mb = int(mem[2])
                    free_ram_mb = total_ram_mb - used_ram_mb
                    total_ram_gb = total_ram_mb / 1024
                    free_ram_gb = free_ram_mb / 1024
                else:
                    total_ram_gb = 0
                    free_ram_gb = 0
                
                # Get CPU cores
                cpu_result = subprocess.run(['nproc'], capture_output=True, text=True)
                total_cpu = int(cpu_result.stdout.strip()) if cpu_result.stdout.strip() else 0
                
                # Get disk space
                disk_result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
                disk_lines = disk_result.stdout.splitlines()
                if len(disk_lines) > 1:
                    disk_parts = disk_lines[1].split()
                    total_disk_str = disk_parts[1]
                    # Convert to GB
                    if 'T' in total_disk_str:
                        total_disk = float(total_disk_str.replace('T', '')) * 1024
                    elif 'G' in total_disk_str:
                        total_disk = float(total_disk_str.replace('G', ''))
                    elif 'M' in total_disk_str:
                        total_disk = float(total_disk_str.replace('M', '')) / 1024
                    else:
                        total_disk = 0
                else:
                    total_disk = 0
                
                # Calculate free resources (simplified - actual would need more complex logic)
                free_cpu = max(0, total_cpu - (total_cpu_allocated // total_nodes))  # Approximate
                free_disk = max(0, total_disk - (total_disk_allocated // total_nodes))  # Approximate
                
                # Update totals
                total_node_ram_allocated += total_ram_gb - free_ram_gb
                total_node_cpu_allocated += total_cpu - free_cpu
                total_node_disk_allocated += total_disk - free_disk
                total_node_ram_free += free_ram_gb
                total_node_cpu_free += free_cpu
                total_node_disk_free += free_disk
                
            except Exception as e:
                logger.error(f"Error getting local node resources: {e}")
                status = "⚠️ Unknown"
                total_node_ram_free = 0
                total_node_cpu_free = 0
                total_node_disk_free = 0
        else:
            # Check remote node status
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if response.status_code == 200:
                    status = "🟢 Online"
                    running_nodes += 1
                else:
                    status = "🔴 Offline"
                    stopped_nodes += 1
            except:
                status = "🔴 Offline"
                stopped_nodes += 1
        
        # Get current VPS count on this node
        node_vps_count = get_current_vps_count(node['id'])
        capacity = node['total_vps']
        usage_percentage = (node_vps_count / capacity * 100) if capacity > 0 else 0
        
        node_statuses.append(
            f"**{node['name']}** ({node_type})\n"
            f"📍 {node['location']} • 📊 {node_vps_count}/{capacity} VPS ({usage_percentage:.0f}%)\n"
            f"Status: {status}"
        )
    
    # Calculate response time
    response_time = (time.time() - start_time) * 1000
    
    # Create main embed
    embed = create_embed(
        title="📊 System Status Dashboard",
        description=f"**{BOT_NAME}** - Complete System Overview\n*Generated in {response_time:.0f}ms*",
        color=0x1a1a1a
    )
    
    # Bot & Uptime Section
    add_field(embed, "🤖 Bot Status", 
        f"**Uptime:** {bot_uptime}\n"
        f"**Latency:** {round(bot.latency * 1000)}ms\n"
        f"**Version:** {BOT_VERSION}\n"
        f"**Developer:** {BOT_DEVELOPER}", 
        True)
    
    # Nodes Section
    add_field(embed, "🌐 Nodes Overview",
        f"**Total Nodes:** {total_nodes}\n"
        f"**Running:** {running_nodes} 🟢\n"
        f"**Stopped:** {stopped_nodes} 🔴\n"
        f"**Local/Remote:** {local_nodes}/{remote_nodes}",
        True)
    
    # VPS & Users Section
    add_field(embed, "👥 Users & VPS",
        f"**Total Users:** {total_users}\n"
        f"**Total VPS:** {total_vps}\n"
        f"**Running:** {running_vps} 🟢\n"
        f"**Stopped:** {stopped_vps} 🔴\n"
        f"**Suspended:** {suspended_vps} 🟡\n"
        f"**Whitelisted:** {whitelisted_vps} ✅",
        True)
    
    # Resources Section - Allocated vs Free
    add_field(embed, "💾 Resource Allocation",
        f"**RAM Allocated:** {total_ram_allocated} GB\n"
        f"**RAM Free:** {total_node_ram_free:.1f} GB\n"
        f"**CPU Allocated:** {total_cpu_allocated} Cores\n"
        f"**CPU Free:** {total_node_cpu_free:.1f} Cores\n"
        f"**Disk Allocated:** {total_disk_allocated} GB\n"
        f"**Disk Free:** {total_node_disk_free:.1f} GB",
        True)
    
    # System & Admin Section
    add_field(embed, "⚙️ System Information",
        f"**Total Admins:** {total_admins}\n"
        f"**Main Admin(s):** {', '.join(f'<@{mid}>' for mid in main_admin_ids)}\n"
        f"**Ports Allocated:** {total_ports_allocated}\n"
        f"**Ports In Use:** {total_ports_used}\n"
        f"**Ports Available:** {total_ports_allocated - total_ports_used}",
        True)
    
    # Node Details Section (if any nodes exist)
    if node_statuses:
        # Split node statuses into chunks if too long
        node_text = "\n\n".join(node_statuses)
        chunks = [node_text[i:i+1024] for i in range(0, len(node_text), 1024)]
        
        for idx, chunk in enumerate(chunks, 1):
            title = "📡 Node Details" if idx == 1 else f"📡 Node Details (Part {idx})"
            add_field(embed, title, chunk, False)
    
    # System Health Indicator
    health_status = "✅ Excellent"
    health_color = 0x00ff88
    
    if running_nodes == 0:
        health_status = "🔴 Critical - No nodes running"
        health_color = 0xff3366
    elif stopped_nodes > 0:
        health_status = "🟡 Warning - Some nodes offline"
        health_color = 0xffaa00
    elif total_vps == 0:
        health_status = "ℹ️ No VPS deployed"
        health_color = 0x00ccff
    
    add_field(embed, "🏥 System Health", health_status, False)
    
    # Footer with current time
    embed.set_footer(text=f"{BOT_NAME} System Status • Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    icon_url="https://i.imgur.com/Tv3clt0.jpeg")
    
    await ctx.send(embed=embed)


@bot.command(name='status-summary')
@is_admin()
async def status_summary(ctx):
    """
    Quick summary of system status
    """
    # Get quick stats
    nodes = get_nodes()
    total_nodes = len(nodes)
    running_nodes = 0
    
    for node in nodes:
        if node['is_local']:
            running_nodes += 1
        else:
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=3)
                if response.status_code == 200:
                    running_nodes += 1
            except:
                pass
    
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())
    total_users = len(vps_data)
    
    # Count VPS status
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    
    for vps_list in vps_data.values():
        for vps in vps_list:
            if vps.get('suspended', False):
                suspended_vps += 1
            elif vps.get('status') == 'running':
                running_vps += 1
            else:
                stopped_vps += 1
    
    embed = create_success_embed(
        "📈 Quick Status Summary",
        f"**Nodes:** {running_nodes}/{total_nodes} 🟢\n"
        f"**VPS:** {total_vps} total\n"
        f"• Running: {running_vps} 🟢\n"
        f"• Stopped: {stopped_vps} 🔴\n"
        f"• Suspended: {suspended_vps} 🟡\n"
        f"**Users:** {total_users} 👥\n"
        f"**Bot Latency:** {round(bot.latency * 1000)}ms"
    )
    
    embed.set_footer(text=f"Use '{PREFIX}status' for detailed information")
    await ctx.send(embed=embed)

@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Already Admin", "This user is already the main admin!"))
        return
    if user_id in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Already Admin", f"{user.mention} is already an admin!"))
        return
    admin_data["admins"].append(user_id)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin!"))
    try:
        await user.send(embed=create_embed("🎉 Admin Role Granted", f"You are now an admin by {ctx.author.mention}", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {user.mention}"))

@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Cannot Remove", "You cannot remove the main admin!"))
        return
    if user_id not in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Not Admin", f"{user.mention} is not an admin!"))
        return
    admin_data["admins"].remove(user_id)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin!"))
    try:
        await user.send(embed=create_embed("⚠️ Admin Role Revoked", f"Your admin role was removed by {ctx.author.mention}", 0xff3366))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {user.mention}"))

@bot.command(name='add-admin')
@is_main_admin()
async def add_admin_id(ctx, user_id: str):
    """Add a main admin by raw Discord user ID (works even if the user isn't in this server)."""
    if not user_id.isdigit():
        await ctx.send(embed=create_error_embed("Invalid ID", "Please provide a numeric Discord user ID."))
        return
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Already Admin", f"`{user_id}` is already a main admin!"))
        return
    main_admin_ids.add(user_id)
    save_main_admins()
    await ctx.send(embed=create_success_embed("Main Admin Added", f"`{user_id}` is now a main admin!"))
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=create_embed("🎉 Main Admin Access Granted", f"You are now a main admin of {BOT_NAME}, granted by {ctx.author.mention}", 0x00ff88))
    except Exception:
        pass

@bot.command(name='rm-admin')
@is_main_admin()
async def rm_admin_id(ctx, user_id: str):
    """Remove a main admin by raw Discord user ID."""
    if user_id not in main_admin_ids:
        await ctx.send(embed=create_error_embed("Not Admin", f"`{user_id}` is not a main admin!"))
        return
    if len(main_admin_ids) <= 1:
        await ctx.send(embed=create_error_embed("Cannot Remove", "At least one main admin must remain."))
        return
    main_admin_ids.discard(user_id)
    save_main_admins()
    await ctx.send(embed=create_success_embed("Main Admin Removed", f"`{user_id}` is no longer a main admin!"))
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=create_embed("⚠️ Main Admin Access Revoked", f"Your main admin role was removed by {ctx.author.mention}", 0xff3366))
    except Exception:
        pass

@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = admin_data.get("admins", [])
    embed = create_embed("👑 Admin Team", "Current administrators:", 0x1a1a1a)
    main_admin_list = []
    for main_id in main_admin_ids:
        try:
            main_user = await bot.fetch_user(int(main_id))
            main_admin_list.append(f"• {main_user.mention} (ID: {main_id})")
        except:
            main_admin_list.append(f"• Unknown User (ID: {main_id})")
    add_field(embed, "🔰 Main Admin(s)", "\n".join(main_admin_list) or "None set", False)
    if admins:
        admin_list = []
        for admin_id in admins:
            try:
                admin_user = await bot.fetch_user(int(admin_id))
                admin_list.append(f"• {admin_user.mention} (ID: {admin_id})")
            except:
                admin_list.append(f"• Unknown User (ID: {admin_id})")
        admin_text = "\n".join(admin_list)
        add_field(embed, "🛡️ Admins", admin_text, False)
    else:
        add_field(embed, "🛡️ Admins", "No additional admins", False)
    await ctx.send(embed=embed)

@bot.command(name="userinfo")
@is_admin()
async def user_info(ctx, user: discord.Member):
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])

    # ─── Embed ─────────────────────────────────────────────────
    embed = create_embed(
        title="👤 User Dashboard",
        description=f"Statistics & resources for {user.mention}",
        color=0x1A1A1A
    )

    # ─── Row 1 : User Info ─────────────────────────────────────
    embed.add_field(
        name="👤 User",
        value=(
            f"**Name:** `{user.name}`\n"
            f"**ID:** `{user.id}`\n"
            f"**Joined:** `{user.joined_at.strftime('%Y-%m-%d') if user.joined_at else 'Unknown'}`"
        ),
        inline=True
    )

    is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
    embed.add_field(
        name="🛡️ Admin",
        value="✅ Yes" if is_admin_user else "❌ No",
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS Count",
        value=f"`{len(vps_list)}` VPS",
        inline=True
    )

    # ─── If VPS Exists ─────────────────────────────────────────
    if vps_list:
        total_ram = total_cpu = total_storage = 0
        running = suspended = whitelisted = 0

        vps_lines = []

        for i, vps in enumerate(vps_list, start=1):
            node = get_node(vps.get("node_id"))
            node_name = node["name"] if node else "Unknown"

            ram = int(vps.get("ram", "0GB").replace("GB", ""))
            storage = int(vps.get("storage", "0GB").replace("GB", ""))
            cpu = int(vps.get("cpu", 0))

            total_ram += ram
            total_storage += storage
            total_cpu += cpu

            if vps.get("suspended"):
                status = "⛔ SUSPENDED"
                suspended += 1
            elif vps.get("status") == "running":
                status = "🟢 RUNNING"
                running += 1
            else:
                status = "🔴 STOPPED"

            if vps.get("whitelisted"):
                whitelisted += 1

            vps_lines.append(
                f"**{i}.** `{vps['container_name']}`\n"
                f"{status} | `{ram}GB` RAM • `{cpu}` CPU • `{storage}GB` Disk\n"
                f"📍 Node: `{node_name}`"
            )

        # ─── Row 2 : VPS Summary ────────────────────────────────
        embed.add_field(
            name="📊 VPS Summary",
            value=(
                f"🖥️ `{len(vps_list)}` Total\n"
                f"🟢 `{running}` Running\n"
                f"⛔ `{suspended}` Suspended\n"
                f"✅ `{whitelisted}` Whitelisted"
            ),
            inline=True
        )

        embed.add_field(
            name="📈 Resources",
            value=(
                f"**RAM:** `{total_ram} GB`\n"
                f"**CPU:** `{total_cpu} Cores`\n"
                f"**Disk:** `{total_storage} GB`"
            ),
            inline=True
        )

        port_quota = get_user_allocation(user_id)
        port_used = get_user_used_ports(user_id)

        embed.add_field(
            name="🌐 Ports",
            value=f"`{port_used}/{port_quota}` Used",
            inline=True
        )

        # ─── VPS List (Split if needed) ────────────────────────
        vps_text = "\n\n".join(vps_lines)
        for i in range(0, len(vps_text), 1024):
            embed.add_field(
                name="📋 VPS List",
                value=vps_text[i:i + 1024],
                inline=False
            )

    else:
        embed.add_field(
            name="🖥️ VPS",
            value="❌ No VPS assigned",
            inline=False
        )

    embed.set_footer(text="UnixNodes • User Resource Dashboard")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name="serverstats")
@is_admin()
async def server_stats(ctx):
    # ─── Counts ────────────────────────────────────────────────
    total_users = len(vps_data)
    total_admins = len(admin_data.get("admins", [])) + 1
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())

    total_ram = total_cpu = total_storage = 0
    running_vps = suspended_vps = stopped_vps = 0
    whitelisted_vps = 0

    # ─── VPS Data ──────────────────────────────────────────────
    for vps_list in vps_data.values():
        for vps in vps_list:
            total_ram += int(vps.get("ram", "0GB").replace("GB", ""))
            total_storage += int(vps.get("storage", "0GB").replace("GB", ""))
            total_cpu += int(vps.get("cpu", 0))

            if vps.get("status") == "running":
                if vps.get("suspended", False):
                    suspended_vps += 1
                else:
                    running_vps += 1
            else:
                stopped_vps += 1

            if vps.get("whitelisted", False):
                whitelisted_vps += 1

    # ─── Ports ─────────────────────────────────────────────────
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()

    # ─── Embed ─────────────────────────────────────────────────
    embed = create_embed(
        title="📊 Server Statistics",
        description="**Live Infrastructure Dashboard**",
        color=0x1A1A1A
    )

    # ── Row 1 ──────────────────────────────────────────────────
    embed.add_field(
        name="👥 Users",
        value=f"`{total_users}` Users\n`{total_admins}` Admins",
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS",
        value=(
            f"Total: `{total_vps}`\n"
            f"🟢 `{running_vps}` Running\n"
            f"⛔ `{suspended_vps}` Suspended"
        ),
        inline=True
    )

    embed.add_field(
        name="📌 Status",
        value=(
            f"🔴 `{stopped_vps}` Stopped\n"
            f"✅ `{whitelisted_vps}` Whitelisted"
        ),
        inline=True
    )

    # ── Row 2 ──────────────────────────────────────────────────
    embed.add_field(
        name="📈 RAM",
        value=f"`{total_ram} GB`",
        inline=True
    )

    embed.add_field(
        name="⚙️ CPU",
        value=f"`{total_cpu} Cores`",
        inline=True
    )

    embed.add_field(
        name="💾 Storage",
        value=f"`{total_storage} GB`",
        inline=True
    )

    # ── Row 3 ──────────────────────────────────────────────────
    embed.add_field(
        name="🌐 Ports Allocated",
        value=f"`{total_ports_allocated}`",
        inline=True
    )

    embed.add_field(
        name="🔌 Ports In Use",
        value=f"`{total_ports_used}`",
        inline=True
    )

    embed.add_field(
        name="📊 Utilization",
        value=(
            f"`{total_ports_used}/{total_ports_allocated}`"
            if total_ports_allocated else "`N/A`"
        ),
        inline=True
    )

    embed.set_footer(text="UnixNodes • Real-Time Monitoring")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    if not container_name:
        all_vps = []
        for user_id, vps_list in vps_data.items():
            try:
                user = await bot.fetch_user(int(user_id))
                for i, vps in enumerate(vps_list):
                    node = get_node(vps['node_id'])
                    node_name = node['name'] if node else "Unknown"
                    status_text = vps.get('status', 'unknown').upper()
                    if vps.get('suspended', False):
                        status_text += " (SUSPENDED)"
                    if vps.get('whitelisted', False):
                        status_text += " (WHITELISTED)"
                    all_vps.append(f"**{user.name}** - VPS {i+1}: `{vps['container_name']}` - {status_text} (Node: {node_name})")
            except:
                pass
        vps_text = "\n".join(all_vps)
        chunks = [vps_text[i:i+1024] for i in range(0, len(vps_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"🖥️ All VPS (Part {idx})", f"List of all VPS deployments", 0x1a1a1a)
            add_field(embed, "VPS List", chunk, False)
            await ctx.send(embed=embed)
    else:
        found_vps = None
        found_user = None
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    found_vps = vps
                    found_user = await bot.fetch_user(int(user_id))
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
            return
        node = get_node(found_vps['node_id'])
        node_name = node['name'] if node else "Unknown"
        suspended_text = " (SUSPENDED)" if found_vps.get('suspended', False) else ""
        whitelisted_text = " (WHITELISTED)" if found_vps.get('whitelisted', False) else ""
        embed = create_embed(f"🖥️ VPS Information - {container_name}", f"Details for VPS owned by {found_user.mention}{suspended_text}{whitelisted_text} on node {node_name}", 0x1a1a1a)
        add_field(embed, "👤 Owner", f"**Name:** {found_user.name}\n**ID:** {found_user.id}", False)
        add_field(embed, "📊 Specifications", f"**RAM:** {found_vps['ram']}\n**CPU:** {found_vps['cpu']} Cores\n**Storage:** {found_vps['storage']}", False)
        add_field(embed, "📈 Status", f"**Current:** {found_vps.get('status', 'unknown').upper()}{suspended_text}{whitelisted_text}\n**Suspended:** {found_vps.get('suspended', False)}\n**Whitelisted:** {found_vps.get('whitelisted', False)}\n**Created:** {found_vps.get('created_at', 'Unknown')}", False)
        if 'config' in found_vps:
            add_field(embed, "⚙️ Configuration", f"**Config:** {found_vps['config']}", False)
        if found_vps.get('shared_with'):
            shared_users = []
            for shared_id in found_vps['shared_with']:
                try:
                    shared_user = await bot.fetch_user(int(shared_id))
                    shared_users.append(f"• {shared_user.mention}")
                except:
                    shared_users.append(f"• Unknown User ({shared_id})")
            shared_text = "\n".join(shared_users)
            add_field(embed, "🔗 Shared With", shared_text, False)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM port_forwards WHERE vps_container = ?', (container_name,))
        port_count = cur.fetchone()[0]
        conn.close()
        add_field(embed, "🌐 Active Ports", f"{port_count} forwarded ports (TCP/UDP)", False)
        await ctx.send(embed=embed)

@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting VPS `{container_name}`..."))
    try:
        await execute_lxc(container_name, f"restart {container_name}", node_id=node_id)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    save_vps_data()
                    break
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("VPS Restarted", f"VPS `{container_name}` has been restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", f"Error: {str(e)}"))

@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Executing Command", f"Running command in VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{command}\"", node_id=node_id)
        embed = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x1a1a1a)
        if output.strip():
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            add_field(embed, "📤 Output", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", f"Error: {str(e)}"))

@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    embed = create_warning_embed("Stopping All VPS", "⚠️ **WARNING:** This will stop ALL running VPS on all nodes.\n\nThis action cannot be undone. Continue?")
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            try:
                stopped_count = 0
                nodes = get_nodes()
                for node in nodes:
                    if node['is_local']:
                        proc = await asyncio.create_subprocess_exec(
                            "lxc", "stop", "--all", "--force",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await proc.communicate()
                        if proc.returncode != 0:
                            logger.error(f"Failed to stop all on local node: {stderr.decode()}")
                            continue
                    else:
                        url = f"{node['url']}/api/execute"
                        data = {"command": "lxc stop --all --force"}
                        params = {"api_key": node["api_key"]}
                        response = requests.post(url, json=data, params=params)
                        if response.status_code != 200:
                            logger.error(f"Failed to stop all on node {node['name']}")
                            continue
                    for user_id, vps_list in vps_data.items():
                        for vps in vps_list:
                            if vps.get('node_id') == node['id'] and vps.get('status') == 'running':
                                vps['status'] = 'stopped'
                                vps['suspended'] = False
                                stopped_count += 1
                save_vps_data()
                embed = create_success_embed("All VPS Stopped", f"Successfully stopped {stopped_count} VPS across all nodes.")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                embed = create_error_embed("Error", f"Error stopping VPS: {str(e)}")
                await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Operation Cancelled", "The stop all VPS operation has been cancelled."))

    await ctx.send(embed=embed, view=ConfirmView())

@bot.command(name='cpu-monitor')
@is_admin()
async def resource_monitor_control(ctx, action: str = "status"):
    global resource_monitor_active
    if action.lower() == "status":
        status = "Active" if resource_monitor_active else "Inactive"
        embed = create_embed("Resource Monitor Status", f"Resource monitoring is currently **{status}** (logs only; no auto-stop)", 0x00ccff if resource_monitor_active else 0xffaa00)
        add_field(embed, "Thresholds", f"{CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM usage", True)
        add_field(embed, "Check Interval", f"60 seconds (all nodes)", True)
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        resource_monitor_active = True
        await ctx.send(embed=create_success_embed("Resource Monitor Enabled", "Resource monitoring has been enabled."))
    elif action.lower() == "disable":
        resource_monitor_active = False
        await ctx.send(embed=create_warning_embed("Resource Monitor Disabled", "Resource monitoring has been disabled."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}cpu-monitor <status|enable|disable>`"))

@bot.command(name='resize-vps')
@is_admin()
async def resize_vps(ctx, container_name: str, ram: int = None, cpu: int = None, disk: int = None):
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Please specify at least one resource to resize (ram, cpu, or disk)"))
        return
    found_vps = None
    user_id = None
    vps_index = None
    for uid, vps_list in vps_data.items():
        for i, vps in enumerate(vps_list):
            if vps['container_name'] == container_name:
                found_vps = vps
                user_id = uid
                vps_index = i
                break
        if found_vps:
            break
    if not found_vps:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
        return
    node_id = found_vps['node_id']
    was_running = found_vps.get('status') == 'running' and not found_vps.get('suspended', False)
    disk_changed = disk is not None
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping VPS", f"Stopping VPS `{container_name}` to apply resource changes..."))
        try:
            await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
            found_vps['status'] = 'stopped'
            save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", f"Error stopping VPS: {str(e)}"))
            return
    changes = []
    try:
        new_ram = int(found_vps['ram'].replace('GB', ''))
        new_cpu = int(found_vps['cpu'])
        new_disk = int(found_vps['storage'].replace('GB', ''))
        if ram is not None and ram > 0:
            new_ram = ram
            ram_mb = ram * 1024
            await execute_lxc(container_name, f"config set {container_name} limits.memory {ram_mb}MB", node_id=node_id)
            changes.append(f"RAM: {ram}GB")
        if cpu is not None and cpu > 0:
            new_cpu = cpu
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {cpu}", node_id=node_id)
            changes.append(f"CPU: {cpu} cores")
        if disk is not None and disk > 0:
            new_disk = disk
            await execute_lxc(container_name, f"config device set {container_name} root size={disk}GB", node_id=node_id)
            changes.append(f"Disk: {disk}GB")
        found_vps['ram'] = f"{new_ram}GB"
        found_vps['cpu'] = str(new_cpu)
        found_vps['storage'] = f"{new_disk}GB"
        found_vps['config'] = f"{new_ram}GB RAM / {new_cpu} CPU / {new_disk}GB Disk"
        vps_data[user_id][vps_index] = found_vps
        save_vps_data()
        if was_running:
            await safe_start_container(container_name, node_id)
            found_vps['status'] = 'running'
            save_vps_data()
            await apply_internal_permissions(container_name, node_id)
            await recreate_port_forwards(container_name)
        embed = create_success_embed("VPS Resized", f"Successfully resized resources for VPS `{container_name}`")
        add_field(embed, "Changes Applied", "\n".join(changes), False)
        if disk_changed:
            add_field(embed, "Disk Note", "Run `sudo resize2fs /` inside the VPS to expand the filesystem.", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Resize Failed", f"Error: {str(e)}"))

@bot.command(name='clone-vps')
@is_admin()
async def clone_vps(ctx, container_name: str, new_name: str = None):
    if not new_name:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        new_name = f"{BOT_NAME.lower()}-{container_name}-clone-{timestamp}"
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Cloning VPS", f"Cloning VPS `{container_name}` to `{new_name}`..."))
    try:
        found_vps = None
        user_id = None
        for uid, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    found_vps = vps
                    user_id = uid
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
            return
        await execute_lxc(container_name, f"copy {container_name} {new_name}", node_id=node_id)
        await apply_lxc_config(new_name, node_id)
        await execute_lxc(new_name, f"start {new_name}", node_id=node_id)
        await apply_internal_permissions(new_name, node_id)
        await recreate_port_forwards(new_name)
        if user_id not in vps_data:
            vps_data[user_id] = []
        new_vps = found_vps.copy()
        new_vps['container_name'] = new_name
        new_vps['status'] = 'running'
        new_vps['suspended'] = False
        new_vps['whitelisted'] = False
        new_vps['suspension_history'] = []
        new_vps['created_at'] = datetime.now().isoformat()
        new_vps['shared_with'] = []
        new_vps['id'] = None
        vps_data[user_id].append(new_vps)
        save_vps_data()
        embed = create_success_embed("VPS Cloned", f"Successfully cloned VPS `{container_name}` to `{new_name}`")
        add_field(embed, "New VPS Details", f"**RAM:** {new_vps['ram']}\n**CPU:** {new_vps['cpu']} Cores\n**Storage:** {new_vps['storage']}", False)
        add_field(embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Clone Failed", f"Error: {str(e)}"))

@bot.command(name='migrate-vps')
@is_admin()
async def migrate_vps(ctx, container_name: str, target_node_id: int):
    node_id = find_node_id_for_container(container_name)
    target_node = get_node(target_node_id)
    if not target_node:
        await ctx.send(embed=create_error_embed("Invalid Node", "Target node not found."))
        return
    await ctx.send(embed=create_info_embed("Migrating VPS", f"Migrating VPS `{container_name}` to node {target_node['name']}..."))
    try:
        await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
        temp_name = f"{BOT_NAME.lower()}-{container_name}-temp-{int(time.time())}"
        await execute_lxc(container_name, f"copy {container_name} {temp_name} -s {DEFAULT_STORAGE_POOL}", node_id=target_node_id)
        await execute_lxc(container_name, f"delete {container_name} --force", node_id=node_id)
        await execute_lxc(temp_name, f"rename {temp_name} {container_name}", node_id=target_node_id)
        await apply_lxc_config(container_name, target_node_id)
        await safe_start_container(container_name, target_node_id)
        await apply_internal_permissions(container_name, target_node_id)
        await recreate_port_forwards(container_name)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['node_id'] = target_node_id
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Migrated", f"Successfully migrated VPS `{container_name}` to node {target_node['name']}"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Migration Failed", f"Error: {str(e)}"))

@bot.command(name='vps-stats')
@is_admin()
async def vps_stats(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Statistics", f"Collecting statistics for VPS `{container_name}`..."))
    try:
        stats = await get_container_stats(container_name, node_id)
        embed = create_embed(f"📊 VPS Statistics - {container_name}", f"Resource usage statistics", 0x1a1a1a)
        add_field(embed, "📈 Status", f"**{stats['status'].upper()}**", False)
        add_field(embed, "💻 CPU Usage", f"**{stats['cpu']:.1f}%**", True)
        add_field(embed, "🧠 Memory Usage", f"**{stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)**", True)
        add_field(embed, "💾 Disk Usage", f"**{stats['disk']}**", True)
        add_field(embed, "⏱️ Uptime", f"**{stats['uptime']}**", True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Statistics Failed", f"Error: {str(e)}"))


@bot.command(name='node-check')
@is_admin()
async def node_check(ctx, node_id: int):
    """Check node status and available storage pools"""
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID {node_id} not found."))
        return
    
    embed = create_info_embed(f"Node Check - {node['name']}", 
                             f"Checking status and configuration of node {node['name']}...")
    
    # Check if node is reachable
    status = await get_node_status(node_id)
    add_field(embed, "📡 Connection Status", status, False)
    
    if status.startswith("🟢"):
        # Try to get storage pools
        try:
            pools_output = await execute_lxc("", "storage list", node_id=node_id, timeout=30)
            add_field(embed, "💾 Available Storage Pools", f"```{pools_output}```", False)
            
            # Try to get default profile
            try:
                profile_output = await execute_lxc("", "profile list", node_id=node_id, timeout=30)
                add_field(embed, "📋 Available Profiles", f"```{profile_output[:500]}...```", False)
            except Exception as e:
                add_field(embed, "📋 Profiles", f"Error: {str(e)[:200]}", False)
                
        except Exception as e:
            add_field(embed, "💾 Storage Pools", f"Error: {str(e)[:200]}", False)
        
        # Check remote API endpoint
        try:
            test_response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
            add_field(embed, "🔌 API Endpoint", f"✅ Reachable\nURL: {node['url']}", False)
        except Exception as e:
            add_field(embed, "🔌 API Endpoint", f"❌ Unreachable\nError: {str(e)[:200]}", False)
    else:
        add_field(embed, "⚠️ Status", "Node is offline or unreachable", False)
    
    await ctx.send(embed=embed)

@bot.command(name='vps-network')
@is_admin()
async def vps_network(ctx, container_name: str, action: str, value: str = None):
    node_id = find_node_id_for_container(container_name)
    if action.lower() not in ["list", "add", "remove", "limit"]:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}vps-network <container> <list|add|remove|limit> [value]`"))
        return
    try:
        if action.lower() == "list":
            output = await execute_lxc(container_name, f"exec {container_name} -- ip addr", node_id=node_id)
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            embed = create_embed(f"🌐 Network Interfaces - {container_name}", "Network configuration", 0x1a1a1a)
            add_field(embed, "Interfaces", f"```\n{output}\n```", False)
            await ctx.send(embed=embed)
        elif action.lower() == "limit" and value:
            await execute_lxc(container_name, f"config device set {container_name} eth0 limits.egress {value}", node_id=node_id)
            await execute_lxc(container_name, f"config device set {container_name} eth0 limits.ingress {value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Limited", f"Set network limit to {value} for `{container_name}`"))
        elif action.lower() == "add" and value:
            await execute_lxc(container_name, f"config device add {container_name} eth1 nic nictype=bridged parent={value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Added", f"Added network interface to VPS `{container_name}` with bridge `{value}`"))
        elif action.lower() == "remove" and value:
            await execute_lxc(container_name, f"config device remove {container_name} {value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Removed", f"Removed network interface `{value}` from VPS `{container_name}`"))
        else:
            await ctx.send(embed=create_error_embed("Invalid Parameters", "Please provide valid parameters for the action"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Network Management Failed", f"Error: {str(e)}"))

@bot.command(name='vps-processes')
@is_admin()
async def vps_processes(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Processes", f"Listing processes in VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- ps aux", node_id=node_id)
        if len(output) > 1000:
            output = output[:1000] + "\n... (truncated)"
        embed = create_embed(f"⚙️ Processes - {container_name}", "Running processes", 0x1a1a1a)
        add_field(embed, "Process List", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Process Listing Failed", f"Error: {str(e)}"))

@bot.command(name='vps-logs')
@is_admin()
async def vps_logs(ctx, container_name: str, lines: int = 50):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Logs", f"Fetching last {lines} lines from VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- journalctl -n {lines}", node_id=node_id)
        if len(output) > 1000:
            output = output[:1000] + "\n... (truncated)"
        embed = create_embed(f"📋 Logs - {container_name}", f"Last {lines} log lines", 0x1a1a1a)
        add_field(embed, "System Logs", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Log Retrieval Failed", f"Error: {str(e)}"))

@bot.command(name='vps-uptime')
@is_admin()
async def vps_uptime(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    uptime = await get_container_uptime(container_name, node_id)
    embed = create_info_embed("VPS Uptime", f"Uptime for `{container_name}`: {uptime}")
    await ctx.send(embed=embed)

@bot.command(name='suspend-vps')
@is_admin()
async def suspend_vps(ctx, container_name: str, *, reason: str = "Admin action"):
    node_id = find_node_id_for_container(container_name)
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if vps.get('status') != 'running':
                    await ctx.send(embed=create_error_embed("Cannot Suspend", "VPS must be running to suspend."))
                    return
                try:
                    await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
                    vps['status'] = 'stopped'
                    vps['suspended'] = True
                    if 'suspension_history' not in vps:
                        vps['suspension_history'] = []
                    vps['suspension_history'].append({
                        'time': datetime.now().isoformat(),
                        'reason': reason,
                        'by': f"{ctx.author.name} ({ctx.author.id})"
                    })
                    save_vps_data()
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Suspend Failed", str(e)))
                    return
                try:
                    owner = await bot.fetch_user(int(uid))
                    embed = create_warning_embed("🚨 VPS Suspended", f"Your VPS `{container_name}` has been suspended by an admin.\n\n**Reason:** {reason}\n\nContact an admin to unsuspend.")
                    await owner.send(embed=embed)
                except Exception as dm_e:
                    logger.error(f"Failed to DM owner {uid}: {dm_e}")
                await ctx.send(embed=create_success_embed("VPS Suspended", f"VPS `{container_name}` suspended. Reason: {reason}"))
                found = True
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='unsuspend-vps')
@is_admin()
async def unsuspend_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if not vps.get('suspended', False):
                    await ctx.send(embed=create_error_embed("Not Suspended", "VPS is not suspended."))
                    return
                try:
                    vps['suspended'] = False
                    vps['status'] = 'running'
                    await safe_start_container(container_name, node_id)
                    await apply_internal_permissions(container_name, node_id)
                    await recreate_port_forwards(container_name)
                    save_vps_data()
                    await ctx.send(embed=create_success_embed("VPS Unsuspended", f"VPS `{container_name}` unsuspended and started."))
                    found = True
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Start Failed", str(e)))
                try:
                    owner = await bot.fetch_user(int(uid))
                    embed = create_success_embed("🟢 VPS Unsuspended", f"Your VPS `{container_name}` has been unsuspended by an admin.\nYou can now manage it again.")
                    await owner.send(embed=embed)
                except Exception as dm_e:
                    logger.error(f"Failed to DM owner {uid} about unsuspension: {dm_e}")
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='suspension-logs')
@is_admin()
async def suspension_logs(ctx, container_name: str = None):
    if container_name:
        found = None
        for lst in vps_data.values():
            for vps in lst:
                if vps['container_name'] == container_name:
                    found = vps
                    break
            if found:
                break
        if not found:
            await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))
            return
        history = found.get('suspension_history', [])
        if not history:
            await ctx.send(embed=create_info_embed("No Suspensions", f"No suspension history for `{container_name}`."))
            return
        embed = create_embed("Suspension History", f"For `{container_name}`")
        text = []
        for h in sorted(history, key=lambda x: x['time'], reverse=True)[:10]:
            t = datetime.fromisoformat(h['time']).strftime('%Y-%m-%d %H:%M:%S')
            text.append(f"**{t}** - {h['reason']} (by {h['by']})")
        add_field(embed, "History", "\n".join(text), False)
        if len(history) > 10:
            add_field(embed, "Note", "Showing last 10 entries.")
        await ctx.send(embed=embed)
    else:
        all_logs = []
        for uid, lst in vps_data.items():
            for vps in lst:
                h = vps.get('suspension_history', [])
                for event in sorted(h, key=lambda x: x['time'], reverse=True):
                    t = datetime.fromisoformat(event['time']).strftime('%Y-%m-%d %H:%M')
                    all_logs.append(f"**{t}** - VPS `{vps['container_name']}` (Owner: <@{uid}>) - {event['reason']} (by {event['by']})")
        if not all_logs:
            await ctx.send(embed=create_info_embed("No Suspensions", "No suspension events recorded."))
            return
        logs_text = "\n".join(all_logs)
        chunks = [logs_text[i:i+1024] for i in range(0, len(logs_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"Suspension Logs (Part {idx})", f"Global suspension events (newest first)")
            add_field(embed, "Events", chunk, False)
            await ctx.send(embed=embed)

@bot.command(name='apply-permissions')
@is_admin()
async def apply_permissions(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Applying Permissions", f"Applying advanced permissions to `{container_name}`..."))
    try:
        status = await get_container_status(container_name, node_id)
        was_running = status == 'running'
        if was_running:
            await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
        await apply_lxc_config(container_name, node_id)
        await safe_start_container(container_name, node_id)
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        await ctx.send(embed=create_success_embed("Permissions Applied", f"Advanced permissions applied to VPS `{container_name}`. Docker-ready with unprivileged ports!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Apply Failed", f"Error: {str(e)}"))

@bot.command(name='resource-check')
@is_admin()
async def resource_check(ctx):
    suspended_count = 0
    embed = create_info_embed("Resource Check", "Checking all running VPS for high resource usage...")
    msg = await ctx.send(embed=embed)
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps.get('status') == 'running' and not vps.get('suspended', False) and not vps.get('whitelisted', False):
                container = vps['container_name']
                node_id = vps['node_id']
                stats = await get_container_stats(container, node_id)
                cpu = stats['cpu']
                ram = stats['ram']['pct']
                if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
                    reason = f"High resource usage: CPU {cpu:.1f}%, RAM {ram:.1f}% (threshold: {CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM)"
                    logger.warning(f"Suspending {container}: {reason}")
                    try:
                        await execute_lxc(container, f"stop {container}", node_id=node_id)
                        vps['status'] = 'stopped'
                        vps['suspended'] = True
                        if 'suspension_history' not in vps:
                            vps['suspension_history'] = []
                        vps['suspension_history'].append({
                            'time': datetime.now().isoformat(),
                            'reason': reason,
                            'by': 'Manual Resource Check'
                        })
                        save_vps_data()
                        try:
                            owner = await bot.fetch_user(int(user_id))
                            warn_embed = create_warning_embed("🚨 VPS Auto-Suspended", f"Your VPS `{container}` has been suspended due to high resource usage.\n\n**Reason:** {reason}\n\nContact admin to unsuspend and address the issue.")
                            await owner.send(embed=warn_embed)
                        except Exception as dm_e:
                            logger.error(f"Failed to DM owner {user_id}: {dm_e}")
                        suspended_count += 1
                    except Exception as e:
                        logger.error(f"Failed to suspend {container}: {e}")
    final_embed = create_info_embed("Resource Check Complete", f"Checked all VPS. Suspended {suspended_count} high-usage VPS.")
    await msg.edit(embed=final_embed)

@bot.command(name='whitelist-vps')
@is_admin()
async def whitelist_vps(ctx, container_name: str, action: str):
    if action.lower() not in ['add', 'remove']:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}whitelist-vps <container> <add|remove>`"))
        return
    found = False
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                if action.lower() == 'add':
                    vps['whitelisted'] = True
                    msg = "added to whitelist (exempt from auto-suspension)"
                else:
                    vps['whitelisted'] = False
                    msg = "removed from whitelist"
                save_vps_data()
                await ctx.send(embed=create_success_embed("Whitelist Updated", f"VPS `{container_name}` {msg}."))
                found = True
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='snapshot')
@is_admin()
async def snapshot_vps(ctx, container_name: str, snap_name: str = "snap0"):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Creating Snapshot", f"Creating snapshot '{snap_name}' for `{container_name}`..."))
    try:
        await execute_lxc(container_name, f"snapshot {container_name} {snap_name}", node_id=node_id)
        await ctx.send(embed=create_success_embed("Snapshot Created", f"Snapshot '{snap_name}' created for VPS `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Snapshot Failed", f"Error: {str(e)}"))

@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    try:
        result = await execute_lxc(container_name, f"snapshot list {container_name}", node_id=node_id)
        embed = create_info_embed(f"Snapshots for {container_name}", result)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("List Failed", f"Error: {str(e)}"))

@bot.command(name='restore-snapshot')
@is_admin()
async def restore_snapshot(ctx, container_name: str, snap_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_warning_embed("Restore Snapshot", f"Restoring snapshot '{snap_name}' for `{container_name}` will overwrite current state. Continue?"))
    class RestoreConfirm(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
            await inter.response.defer()
            try:
                await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
                await execute_lxc(container_name, f"restore {container_name} {snap_name}", node_id=node_id)
                await safe_start_container(container_name, node_id)
                await apply_internal_permissions(container_name, node_id)
                await recreate_port_forwards(container_name)
                for uid, lst in vps_data.items():
                    for vps in lst:
                        if vps['container_name'] == container_name:
                            vps['status'] = 'running'
                            vps['suspended'] = False
                            save_vps_data()
                            break
                await inter.followup.send(embed=create_success_embed("Snapshot Restored", f"Restored '{snap_name}' for VPS `{container_name}`."))
            except Exception as e:
                await inter.followup.send(embed=create_error_embed("Restore Failed", f"Error: {str(e)}"))

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
            await inter.response.edit_message(embed=create_info_embed("Cancelled", "Snapshot restore cancelled."))

    await ctx.send(view=RestoreConfirm())

@bot.command(name='backup-db')
@is_admin()
async def backup_db(ctx):
    backup_name = f"vps_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    try:
        shutil.copy('vps.db', backup_name)
        if os.path.exists('vps.db-wal'):
            shutil.copy('vps.db-wal', f"{backup_name}-wal")
        if os.path.exists('vps.db-shm'):
            shutil.copy('vps.db-shm', f"{backup_name}-shm")
        await ctx.send(embed=create_success_embed("DB Backup Created", f"Backup saved as `{backup_name}` (and WAL/SHM if applicable)."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", f"Error: {str(e)}"))

@bot.command(name='repair-ports')
@is_admin()
async def repair_ports(ctx, container_name: str):
    await ctx.send(embed=create_info_embed("Repairing Ports", f"Re-adding port forward devices for `{container_name}`..."))
    try:
        readded = await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("Ports Repaired", f"Re-added {readded} port forwards for `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Repair Failed", f"Error: {str(e)}"))


async def _ipv4_diagnose_container(container_name: str) -> Dict[str, Any]:
    """Gather evidence for IPv4/SSH forwarding without changing the VPS."""
    if not container_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", container_name):
        raise ValueError("Invalid container name")
    # Do not use find_node_id_for_container()'s legacy fallback to node 1 here:
    # that could accidentally diagnose or repair an unrelated instance.
    db = get_db()
    try:
        vps_row = db.execute("SELECT node_id FROM vps WHERE container_name = ?", (container_name,)).fetchone()
    finally:
        db.close()
    if not vps_row:
        raise RuntimeError(f"Container `{container_name}` is not registered in this bot's VPS database. Refusing to touch a potentially unrelated LXD instance.")
    node_id = int(vps_row["node_id"])
    node = get_node(node_id)
    if not node:
        raise RuntimeError(f"Node {node_id} for this VPS was not found in the bot database")
    result: Dict[str, Any] = {"container": container_name, "node_id": node_id, "node_name": node.get("name", "unknown"), "checks": []}
    # Verify this exact instance exists on the selected LXD node.
    try:
        info = await execute_lxc(container_name, f"info {shlex.quote(container_name)}", node_id=node_id, timeout=20)
        result["checks"].append(("LXD instance", True, "Instance exists on the configured node"))
    except Exception as exc:
        result["checks"].append(("LXD instance", False, str(exc)[:500]))
        result["checks"].append(("SSH service", False, "Not checked because the instance was not found/reachable on its configured node"))
        result["checks"].append(("Port forward", False, "Not repaired: target instance could not be verified"))
        return result
    # Check the guest's SSH listener, rather than checking the bot host's localhost.
    guest_check = "if command -v ss >/dev/null 2>&1; then ss -lnt; elif command -v netstat >/dev/null 2>&1; then netstat -lnt; else echo NO_LISTENER_TOOL; fi"
    try:
        listeners = await execute_lxc(container_name, f"exec {shlex.quote(container_name)} -- bash -lc {shlex.quote(guest_check)}", node_id=node_id, timeout=20)
        ssh_open = bool(re.search(r"(?:0\.0\.0\.0|\*|\[::\]|::):22\b|127\.0\.0\.1:22\b", str(listeners)))
        result["checks"].append(("Guest SSH listener", ssh_open, "Port 22 is listening" if ssh_open else f"Port 22 listener not confirmed. Output: {str(listeners)[:350]}"))
    except Exception as exc:
        ssh_open = False
        result["checks"].append(("Guest SSH listener", False, str(exc)[:350]))
    # Only report known, persisted mappings; don't invent a destination or port.
    conn = get_db()
    try:
        forwards = conn.execute("SELECT id, vps_port, host_port FROM port_forwards WHERE vps_container = ? ORDER BY host_port", (container_name,)).fetchall()
    finally:
        conn.close()
    result["forwards"] = [dict(row) for row in forwards]
    result["public_ip"] = get_public_ip(node_id)
    if forwards:
        result["checks"].append(("Saved port mappings", True, f"{len(forwards)} mapping(s) found in bot database"))
    else:
        result["checks"].append(("Saved port mappings", False, "No port mappings stored for this exact container in the bot database"))
    if not result["public_ip"]:
        result["checks"].append(("Public IPv4", False, "Not configured/detected for this node; configure YOUR_SERVER_IP or NODE_PUBLIC_IPS"))
    else:
        result["checks"].append(("Public IPv4", True, result["public_ip"]))
    result["checks"].append(("External reachability", None, "Cannot be proven from inside the guest/node. Test from a separate network and verify provider firewall/NAT."))
    return result


def _ipv4_report_embed(title: str, report: Dict[str, Any], *, repaired: Optional[int] = None):
    status = []
    for label, ok, detail in report.get("checks", []):
        icon = "✅" if ok is True else ("❌" if ok is False else "⚠️")
        status.append(f"{icon} **{label}:** {detail}")
    forwards = report.get("forwards", [])
    if forwards:
        status.append("**Saved forwards:** " + ", ".join(f"`{x['host_port']} → {x['vps_port']}`" for x in forwards[:12]))
    if repaired is not None:
        status.append(f"**Automatic repair:** re-created {repaired} saved mapping(s).")
    status.append("**Important:** an AI/automatic repair cannot open a cloud-provider firewall or create upstream NAT rules without provider access. Verify from an external network.")
    embed = create_info_embed(title, "\n".join(status)[:3900])
    add_field(embed, "Node / VPS", f"`{report.get('node_name', 'unknown')}` / `{report.get('container', 'unknown')}`", False)
    add_field(embed, "Public IPv4", f"`{report.get('public_ip') or 'Not configured'}`", True)
    return embed


@bot.command(name='ipv4-check', aliases=['ipv4-diagnose', 'ssh-check'])
@is_admin()
async def ipv4_check(ctx, container_name: str):
    """Diagnose the real VPS target, SSH listener, saved forwards and public IP."""
    msg = await ctx.send(embed=create_info_embed("🧠 IPv4 Auto-Diagnostics", f"Checking LXD target, guest SSH and saved port mappings for `{container_name}`. No settings will be changed."))
    try:
        report = await _ipv4_diagnose_container(container_name)
        await msg.edit(embed=_ipv4_report_embed("🧠 IPv4 / SSH Diagnostic Report", report))
    except Exception as exc:
        logger.exception("IPv4 diagnostic failed for %s", container_name)
        await msg.edit(embed=create_error_embed("IPv4 Diagnostic Failed", f"Could not complete diagnostics: `{str(exc)[:700]}`"))


@bot.command(name='ipv4-fix', aliases=['ipv4-repair', 'ssh-repair'])
@is_admin()
async def ipv4_fix(ctx, container_name: str):
    """Safely repair saved LXD port mappings after confirming the exact instance."""
    msg = await ctx.send(embed=create_info_embed("🛠️ IPv4 Auto-Repair", f"Verifying `{container_name}` and guest SSH before touching saved port mappings…"))
    try:
        report = await _ipv4_diagnose_container(container_name)
        instance_ok = any(label == "LXD instance" and ok is True for label, ok, _ in report.get("checks", []))
        ssh_ok = any(label == "Guest SSH listener" and ok is True for label, ok, _ in report.get("checks", []))
        if not instance_ok:
            await msg.edit(embed=_ipv4_report_embed("⚠️ Auto-Repair Stopped Safely", report))
            return
        if not report.get("forwards"):
            report["checks"].append(("Auto-repair", False, "No saved mappings found. Nothing was created because the intended host ports/destinations cannot be inferred safely."))
            await msg.edit(embed=_ipv4_report_embed("⚠️ No Saved Forwards", report))
            return
        if not ssh_ok and any(int(x["vps_port"]) == 22 for x in report["forwards"]):
            report["checks"].append(("Auto-repair", False, "Guest SSH on port 22 was not confirmed. Existing mappings were left unchanged."))
            await msg.edit(embed=_ipv4_report_embed("⚠️ Auto-Repair Stopped Safely", report))
            return
        repaired = await recreate_port_forwards(container_name)
        report = await _ipv4_diagnose_container(container_name)
        await msg.edit(embed=_ipv4_report_embed("🛠️ IPv4 Repair Result", report, repaired=repaired))
    except Exception as exc:
        logger.exception("IPv4 auto-repair failed for %s", container_name)
        await msg.edit(embed=create_error_embed("IPv4 Auto-Repair Failed", f"No successful repair could be confirmed: `{str(exc)[:700]}`"))

@bot.command(name='about')
async def about(ctx):
    total_users = len(vps_data)
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())
    latency = round(bot.latency * 1000)
    main_admin = await bot.fetch_user(MAIN_ADMIN_ID)
    embed = create_info_embed(f"About {BOT_NAME}", f"Bot information and statistics")
    add_field(embed, "Bot Name", BOT_NAME, True)
    add_field(embed, "Main Owner", main_admin.mention, True)
    add_field(embed, "Developer", BOT_DEVELOPER, True)
    add_field(embed, "Ping", f"{latency}ms", True)
    add_field(embed, "Version", BOT_VERSION, True)
    add_field(embed, "Total VPS", str(total_vps), True)
    add_field(embed, "Total Users", str(total_users), True)
    await ctx.send(embed=embed)


@bot.command(name='quickhelp')
async def quick_help(ctx):
    """Show quick reference for common tasks"""
    user_id = str(ctx.author.id)
    is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
    
    embed = create_info_embed("🚀 Quick Help Reference", 
        "Quick reference for common tasks. Use `{PREFIX}help` for complete command list.")
    
    # Common user tasks
    add_field(embed, "👤 For Users", 
        "• `{PREFIX}myvps` - List your VPS\n"
        "• `{PREFIX}manage` - Start/stop/manage VPS\n"
        "• `!ports` - Manage port forwarding\n"
        "• `!share-user @user 1` - Share VPS #1\n"
        "• `!about` - Bot information", False)
    
    # VPS management
    add_field(embed, "🖥️ VPS Control", 
        "• In `{PREFIX}manage`: Click ▶ to start VPS\n"
        "• In `{PREFIX}manage`: Click ⏸ to stop VPS\n"
        "• In `{PREFIX}manage`: Click 🔑 for SSH access\n"
        "• In `{PREFIX}manage`: Click 📊 for live stats\n"
        "• In `{PREFIX}manage`: Click 🔄 to reinstall OS", False)
    
    # Troubleshooting
    add_field(embed, "🔧 Common Issues", 
        "• Ports not working? Use `!repair-ports <container>` (admin)\n"
        "• VPS suspended? Contact admin to unsuspend\n"
        "• Need more resources? Contact admin for upgrade\n"
        "• SSH not working? Try reinstall with different OS", False)
    
    if is_admin_user:
        add_field(embed, "🛡️ Admin Quick Actions", 
            "• `!create 2 2 20 @user` - Create 2GB/2CPU/20GB VPS\n"
            "• `!userinfo @user` - Check user details\n"
            "• `!node list` - List all nodes\n"
            "• `!serverstats` - System overview\n"
            "• `!suspend-vps <container> <reason>` - Suspend VPS", False)
    
    embed.set_footer(text=f"{BOT_NAME} VPS Manager • Use {PREFIX}help for complete command list")
    await ctx.send(embed=embed)

@bot.command(name='help-search')
async def help_search(ctx, *, search_term: str = None):
    """Search for commands"""
    if not search_term:
        await show_help(ctx)
        return
    
    search_term = search_term.lower()
    user_id = str(ctx.author.id)
    is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
    is_main_admin_user = user_id in main_admin_ids
    
    # Build complete command list based on permissions
    all_commands = []
    
    # User commands (always available)
    user_categories = ["user", "vps", "ports", "system", "bot"]
    for cat in user_categories:
        all_commands.extend(HelpView(ctx).command_categories[cat]["commands"])
    
    # Admin commands
    if is_admin_user:
        all_commands.extend(HelpView(ctx).command_categories["admin"]["commands"])
        all_commands.extend(HelpView(ctx).command_categories["nodes"]["commands"])
    
    # Main admin commands
    if is_main_admin_user:
        all_commands.extend(HelpView(ctx).command_categories["main_admin"]["commands"])
    
    # Search through commands
    matches = []
    for cmd, desc in all_commands:
        if (search_term in cmd.lower() or search_term in desc.lower()):
            matches.append((cmd, desc))
    
    if not matches:
        embed = create_info_embed("🔍 No Results Found",
            f"No commands found matching '{search_term}'. Try a different search term.")
        await ctx.send(embed=embed)
        return
    
    # Show results
    embed = create_info_embed(f"🔍 Search Results for '{search_term}'",
        f"Found {len(matches)} command(s) matching your search.")
    
    # Group matches by category
    results_text = "\n".join([f"**{cmd}** - {desc}" for cmd, desc in matches[:15]])
    add_field(embed, "Matching Commands", results_text, False)
    
    if len(matches) > 15:
        add_field(embed, "Note", f"Showing 15 of {len(matches)} matches. Try a more specific search.", False)
    
    embed.set_footer(text=f"{BOT_NAME} VPS Manager • Use {PREFIX}help for complete list")
    await ctx.send(embed=embed)    

@bot.command(name='node')
@is_admin()
async def node_cmd(ctx, sub: str, *args):
    if sub == 'create':
        await ctx.send("Enter node name:")
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        name = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter location:")
        location = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter total VPS capacity:")
        total_vps_str = (await bot.wait_for('message', check=check)).content.strip()
        try:
            total_vps = int(total_vps_str)
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "Total VPS must be an integer."))
            return
        await ctx.send("Enter tags (comma separated):")
        tags_str = (await bot.wait_for('message', check=check)).content.strip()
        tags = [t.strip() for t in tags_str.split(',') if t.strip()]
        tags_json = json.dumps(tags)
        await ctx.send("Enter node URL (e.g., http://ip:port) or leave blank for local:")
        url_str = (await bot.wait_for('message', check=check)).content.strip()
        url = url_str.rstrip('/') if url_str else None
        if url and not re.fullmatch(r'https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?', url):
            await ctx.send(embed=create_error_embed('Invalid Node URL', 'Use a plain http(s)://hostname:port endpoint without paths, query strings, or credentials.'))
            return
        is_local = 1 if not url else 0
        api_key = None if is_local else ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (name, location, total_vps, tags_json, api_key, url, is_local))
            conn.commit()
            node_id = cur.lastrowid
            embed = create_success_embed("Node Created", f"ID: {node_id}\nName: {name}\nLocation: {location}\nCapacity: {total_vps}\nTags: {', '.join(tags)}")
            if not is_local:
                add_field(embed, "API Key", api_key, False)
                add_field(embed, "URL", url, False)
                add_field(embed, "Setup", f"Run `python node-agent.py --api_key={api_key} --port=PORT` on the node server.")
            await ctx.send(embed=embed)
        except sqlite3.IntegrityError:
            await ctx.send(embed=create_error_embed("Error", "Node name already exists."))
        conn.close()
    elif sub == 'list':
        nodes = get_nodes()
        embed = create_info_embed("Nodes List", "")
        for n in nodes:
            status = "Local" if n['is_local'] else "Down"
            if not n['is_local']:
                if not n.get('url') or not n.get('api_key'):
                    status = 'Not Configured'
                else:
                    try:
                        response = await asyncio.to_thread(
                            requests.get, f"{n['url'].rstrip('/')}/api/ping",
                            params={'api_key': n['api_key']}, timeout=5
                        )
                        status = 'Up' if response.status_code == 200 else 'Down'
                    except Exception:
                        status = 'Down'
            field = f"ID: {n['id']}\nName: {n['name']}\nLocation: {n['location']}\nCapacity: {n['total_vps']}\nTags: {', '.join(n['tags'])}\nStatus: {status}"
            if not n['is_local'] and n.get('url'):
                field += f"\nEndpoint: {n['url']}"
            if not n['is_local'] and (not n.get('url') or not n.get('api_key')):
                field += "\nSetup required: configure a real remote node endpoint and agent API key."
            add_field(embed, f"Node {n['id']}", field, False)
        await ctx.send(embed=embed)
    elif sub == 'edit':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node edit <id>"))
            return
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        await ctx.send(f"Editing node {node['name']}. Enter new name ( . to skip):")
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        new_name = (await bot.wait_for('message', check=check)).content.strip()
        if new_name != '.':
            node['name'] = new_name
        await ctx.send("New location ( . to skip):")
        new_loc = (await bot.wait_for('message', check=check)).content.strip()
        if new_loc != '.':
            node['location'] = new_loc
        await ctx.send("New total VPS capacity ( . to skip):")
        new_total = (await bot.wait_for('message', check=check)).content.strip()
        if new_total != '.':
            node['total_vps'] = int(new_total)
        await ctx.send("New tags (comma separated, . to skip):")
        new_tags = (await bot.wait_for('message', check=check)).content.strip()
        if new_tags != '.':
            node['tags'] = json.dumps([t.strip() for t in new_tags.split(',') if t.strip()])
        if not node['is_local']:
            await ctx.send("New URL ( . to skip):")
            new_url = (await bot.wait_for('message', check=check)).content.strip()
            if new_url != '.':
                node['url'] = new_url
            await ctx.send("Regenerate API key? (y/n):")
            regen = (await bot.wait_for('message', check=check)).content.strip().lower()
            if regen == 'y':
                node['api_key'] = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE nodes SET name=?, location=?, total_vps=?, tags=?, api_key=?, url=? WHERE id=?',
                    (node['name'], node['location'], node['total_vps'], json.dumps(node['tags']), node.get('api_key'), node.get('url'), node_id))
        conn.commit()
        conn.close()
        embed = create_success_embed("Node Updated", f"ID: {node_id}\nName: {node['name']}\nLocation: {node['location']}\nCapacity: {node['total_vps']}\nTags: {', '.join(node['tags'])}")
        if not node['is_local']:
            add_field(embed, "API Key", node['api_key'], False)
            add_field(embed, "URL", node['url'], False)
        await ctx.send(embed=embed)
    
    # NEW: Add delete subcommand
    elif sub == 'delete':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node delete <id> [force]"))
            return
        
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        
        force = False
        if len(args) > 1 and args[1].lower() == 'force':
            force = True
        elif len(args) > 1:
            await ctx.send(embed=create_error_embed("Invalid Argument", "Optional argument must be 'force'."))
            return
        
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        
        # Check if this is the local node
        if node['is_local']:
            await ctx.send(embed=create_error_embed("Cannot Delete", "Cannot delete the local node."))
            return
        
        # Check if node has any VPS assigned
        vps_count = get_current_vps_count(node_id)
        if not force and vps_count > 0:
            await ctx.send(embed=create_error_embed("Cannot Delete", 
                f"Node has {vps_count} VPS assigned. Migrate or delete them first, or use 'force' to delete all VPS and the node."))
            return
        
        # Prepare warning message
        warning_msg = f"Are you sure you want to delete node **{node['name']}** (ID: {node_id})?\n\n"
        warning_msg += f"**Location:** {node['location']}\n"
        warning_msg += f"**Tags:** {', '.join(node['tags'])}\n\n"
        if force and vps_count > 0:
            warning_msg += f"**WARNING: Force mode will delete all {vps_count} VPS on this node first!**\n\n"
        warning_msg += "This action cannot be undone!"
        
        embed = create_warning_embed("⚠️ Delete Node", warning_msg)
        
        class ConfirmDelete(discord.ui.View):
            def __init__(self, node_id, node_name, force, vps_count):
                super().__init__(timeout=60)
                self.node_id = node_id
                self.node_name = node_name
                self.force = force
                self.vps_count = vps_count
            
            @discord.ui.button(label="Delete Node", style=discord.ButtonStyle.danger)
            async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
                if str(inter.user.id) != str(ctx.author.id):
                    await inter.response.send_message(
                        embed=create_error_embed("Access Denied", "Only the command author can confirm."),
                        ephemeral=True
                    )
                    return
                
                await inter.response.defer()
                
                conn = get_db()
                cur = conn.cursor()
                
                if self.force and self.vps_count > 0:
                    # Force delete all VPS on this node
                    cur.execute('DELETE FROM vps WHERE node_id = ?', (self.node_id,))
                
                # Delete the node from database
                cur.execute('DELETE FROM nodes WHERE id = ?', (self.node_id,))
                
                conn.commit()
                conn.close()
                
                msg = f"Node **{self.node_name}** (ID: {self.node_id}) has been deleted."
                if self.force and self.vps_count > 0:
                    msg += f" All {self.vps_count} VPS on the node were also deleted."
                
                success_embed = create_success_embed("Node Deleted", msg)
                await inter.followup.send(embed=success_embed)
                self.stop()
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
                if str(inter.user.id) != str(ctx.author.id):
                    await inter.response.send_message(
                        embed=create_error_embed("Access Denied", "Only the command author can cancel."),
                        ephemeral=True
                    )
                    return
                
                await inter.response.edit_message(
                    embed=create_info_embed("Deletion Cancelled", "Node deletion was cancelled."),
                    view=None
                )
                self.stop()
        
        await ctx.send(embed=embed, view=ConfirmDelete(node_id, node['name'], force, vps_count))
    
    elif sub == 'status':
        # New: Check node status
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node status <id>"))
            return
        
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        
        embed = create_info_embed(f"Node Status - {node['name']}")
        
        if node['is_local']:
            status = "🟢 Local Node"
            cpu_usage = get_host_cpu_usage()
            ram_usage = get_host_ram_usage()
            add_field(embed, "Status", status, True)
            add_field(embed, "CPU Usage", f"{cpu_usage:.1f}%", True)
            add_field(embed, "RAM Usage", f"{ram_usage:.1f}%", True)
        else:
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if response.status_code == 200:
                    status = "🟢 Online"
                    try:
                        stats_response = requests.get(f"{node['url']}/api/get_host_stats", 
                                                    params={'api_key': node['api_key']}, 
                                                    timeout=5)
                        if stats_response.status_code == 200:
                            stats = stats_response.json()
                            cpu_usage = stats.get('cpu', 0.0)
                            ram_usage = stats.get('ram', 0.0)
                            add_field(embed, "CPU Usage", f"{cpu_usage:.1f}%", True)
                            add_field(embed, "RAM Usage", f"{ram_usage:.1f}%", True)
                    except:
                        cpu_usage = "Unknown"
                        ram_usage = "Unknown"
                else:
                    status = "🔴 Offline"
            except:
                status = "🔴 Offline"
            
            add_field(embed, "Status", status, True)
        
        vps_count = get_current_vps_count(node_id)
        capacity = node['total_vps']
        usage_percentage = (vps_count / capacity * 100) if capacity > 0 else 0
        
        add_field(embed, "VPS Capacity", f"{vps_count}/{capacity} ({usage_percentage:.1f}%)", True)
        add_field(embed, "Location", node['location'], True)
        add_field(embed, "Tags", ", ".join(node['tags']), True)
        
        if not node['is_local']:
            add_field(embed, "URL", node['url'], False)
        
        await ctx.send(embed=embed)
    
    else:
        # Show help for node command
        embed = create_info_embed("Node Management", 
            f"Manage multi-node infrastructure for {BOT_NAME}")

class HelpView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.current_category = "user"
        # Command categories
        self.command_categories = {
            "user": {
                "name": "<:user:1555957676813324448> User Commands",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}manage [@user]", "Manage your VPS or another user's VPS (Admin only)"),
                    (f"{PREFIX}share-user @user <vps_number>", "Share VPS access"),
                    (f"{PREFIX}share-ruser @user <vps_number>", "Revoke VPS access"),
                    (f"{PREFIX}manage-shared @owner <vps_number>", "Manage shared VPS")
                ]
            },
            "vps": {
                "name": "<:vps:1555957679929950322> VPS Management",
                "commands": [
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}vpsinfo [container]", "VPS information"),
                    (f"{PREFIX}vps-stats <container>", "VPS stats"),
                    (f"{PREFIX}vps-uptime <container>", "VPS uptime"),
                    (f"{PREFIX}vps-processes <container>", "List processes"),
                    (f"{PREFIX}vps-logs <container> [lines]", "Show logs"),
                    (f"{PREFIX}restart-vps <container>", "Restart VPS"),
                    (f"{PREFIX}clone-vps <container> [new_name]", "Clone VPS"),
                    (f"{PREFIX}snapshot <container> [snap_name]", "Create snapshot"),
                    (f"{PREFIX}list-snapshots <container>", "List snapshots"),
                    (f"{PREFIX}restore-snapshot <container> <snap_name>", "Restore snapshot")
                ]
            },
            "ports": {
                "name": "<:ports:1555957616126201931> Port Forwarding",
                "commands": [
                    (f"{PREFIX}ports [add <vps_num> <port> | list | remove <id>]", "Manage port forwards (TCP/UDP)"),
                    (f"{PREFIX}ports-add-user <amount> @user", "Allocate port slots to user (Admin only)"),
                    (f"{PREFIX}ports-remove-user <amount> @user", "Deallocate port slots from user (Admin only)"),
                    (f"{PREFIX}ports-revoke <id>", "Revoke specific port forward (Admin only)")
                ]
            },
            "system": {
                "name": "<:system:1555957661051125883> System Commands",
                "commands": [
                    (f"{PREFIX}serverstats", "Server statistics"),
                    (f"{PREFIX}resource-check", "Check and suspend high-usage VPS (Admin only)"),
                    (f"{PREFIX}cpu-monitor <status|enable|disable>", "Resource monitor control (logging only)"),
                    (f"{PREFIX}thresholds", "View resource thresholds"),
                    (f"{PREFIX}set-threshold <cpu> <ram>", "Set resource thresholds (Admin only)"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin only)"),
                    (f"{PREFIX}host-resources [node_id]", "Host Resource Overview: CPU, RAM and storage capacity/allocations (Admin only)"),
                    (f"{PREFIX}health-check", "Run read-only node/VPS health diagnostics"),
                    (f"{PREFIX}set-log-channel #channel", "Set health/overload log channel"),
                    (f"{PREFIX}set-backup-channel #channel", "Set backup upload channel")
                ]
            },
            "nodes": {
                "name": "<:system:1555957661051125883> Node Management",
                "commands": [
                    (f"{PREFIX}node create", "Create a new node (Admin only)"),
                    (f"{PREFIX}node list", "List all nodes (Admin only)"),
                    (f"{PREFIX}node status <id>", "Check node status (Admin only)"),
                    (f"{PREFIX}node edit <id>", "Edit node details (Admin only)"),
                    (f"{PREFIX}node delete <id>", "Delete a node (Admin only)"),
                    (f"{PREFIX}node migrate <from> <to>", "Migrate VPS between nodes (Admin only)"),
                    (f"{PREFIX}lxc-list [node_id]", "List LXC containers on node (Admin only)")
                ],
                "admin_only": True
            },
            "bot": {
                "name": "<:bot:1555957531493408959> Bot System Commands",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}help", "Show this help menu"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin only)"),
                    (f"{PREFIX}health-check", "Run read-only node/VPS health diagnostics"),
                    (f"{PREFIX}set-log-channel #channel", "Set health/overload log channel"),
                    (f"{PREFIX}set-backup-channel #channel", "Set backup upload channel")
                ]
            },
            "admin": {
                "name": "<:admin:1555957513701167145> Admin Commands",
                "commands": [
                    (f"{PREFIX}lxc-list", "List all LXC containers"),
                    (f"{PREFIX}create <ram_gb> <cpu_cores> <disk_gb> @user", "Create VPS with OS selection"),
                    (f"{PREFIX}delete-vps @user <vps_number> [reason]", "Delete user's VPS"),
                    (f"{PREFIX}add-resources <container> [ram] [cpu] [disk]", "Add resources to VPS"),
                    (f"{PREFIX}resize-vps <container> [ram] [cpu] [disk]", "Resize VPS resources"),
                    (f"{PREFIX}suspend-vps <container> [reason]", "Suspend VPS"),
                    (f"{PREFIX}unsuspend-vps <container>", "Unsuspend VPS"),
                    (f"{PREFIX}suspension-logs [container]", "View suspension logs"),
                    (f"{PREFIX}whitelist-vps <container> <add|remove>", "Whitelist VPS from auto-suspend"),
                    (f"{PREFIX}userinfo @user", "User information"),
                    (f"{PREFIX}list-all", "List all VPS"),
                    (f"{PREFIX}exec <container> <command>", "Execute command"),
                    (f"{PREFIX}stop-vps-all", "Stop all VPS"),
                    (f"{PREFIX}migrate-vps <container> <pool>", "Migrate VPS"),
                    (f"{PREFIX}vps-network <container> <action> [value]", "Network management"),
                    (f"{PREFIX}host-resources [node_id]", "Host Resource Overview (CPU/RAM/storage; Admin only)"),
                    (f"{PREFIX}ipv4-check [container]", "Diagnose public IPv4/SSH connectivity"),
                    (f"{PREFIX}ipv4-fix [container]", "Attempt supported IPv4/SSH repair checks"),
                    (f"{PREFIX}ai-doctor", "AI-assisted diagnostics and recommendations"),
                    (f"{PREFIX}ai-optimize <container>", "Analyze VPS resource usage and suggest optimizations"),
                    (f"{PREFIX}backup-now", "Create and upload metadata backup"),
                    (f"{PREFIX}backup-status", "Show backup status")
                ],
                "admin_only": True
            },
            "main_admin": {
                "name": "<:mainadmin:1555957596899246193> Main Admin Commands",
                "commands": [
                    (f"{PREFIX}admin-add @user", "Add admin (mention)"),
                    (f"{PREFIX}admin-remove @user", "Remove admin (mention)"),
                    (f"{PREFIX}add-admin <user_id>", "Add main admin (by ID)"),
                    (f"{PREFIX}rm-admin <user_id>", "Remove main admin (by ID)"),
                    (f"{PREFIX}admin-list", "List admins"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot presence/status (Admin only)"),
                    (f"{PREFIX}set-threshold <cpu> <ram>", "Configure overload thresholds (Admin only)"),
                    (f"{PREFIX}thresholds", "View current CPU/RAM thresholds"),
                    (f"{PREFIX}backup-now", "Create metadata backup"),
                    (f"{PREFIX}backup-status", "View backup status")
                ],
                "admin_only": True,
                "main_admin_only": True
            }
        }
        self.update_select()
        self.update_embed()
        self.add_item(self.select)

    def update_select(self):
        """Update the category selection dropdown based on user permissions"""
        self.select = discord.ui.Select(placeholder="Select Category", options=[])
        user_id = str(self.ctx.author.id)
        is_admin_user = user_id in main_admin_ids or user_id in admin_data.get("admins", [])
        is_main_admin_user = user_id in main_admin_ids
       
        # Add all categories that user has access to
        options = []
        # Always show basic categories
        basic_categories = ["user", "vps", "ports", "system", "bot"]
        for category in basic_categories:
            options.append(discord.SelectOption(
                label=self.command_categories[category]["name"].split("> ", 1)[-1],
                value=category,
                emoji=self.get_category_emoji(category)
            ))
       
        # Add nodes category if admin
        if is_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["nodes"]["name"].split("> ", 1)[-1],
                value="nodes",
                emoji=self.get_category_emoji("nodes")
            ))
       
        # Add admin categories if user has permissions
        if is_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["admin"]["name"].split("> ", 1)[-1],
                value="admin",
                emoji=self.get_category_emoji("admin")
            ))
       
        if is_main_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["main_admin"]["name"].split("> ", 1)[-1],
                value="main_admin",
                emoji=self.get_category_emoji("main_admin")
            ))
       
        self.select.options = options
        self.select.callback = self.select_callback
   
    async def select_callback(self, interaction: discord.Interaction):
        """Handle category selection"""
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return
        
        self.current_category = interaction.data['values'][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    def get_category_emoji(self, category):
        """Get emoji for each category"""
        emojis = {
            "user": discord.PartialEmoji(name="user", id=1555957676813324448),
            "vps": discord.PartialEmoji(name="vps", id=1555957679929950322),
            "ports": discord.PartialEmoji(name="ports", id=1555957616126201931),
            "system": discord.PartialEmoji(name="system", id=1555957661051125883),
            "bot": discord.PartialEmoji(name="bot", id=1555957531493408959),
            "nodes": discord.PartialEmoji(name="system", id=1555957661051125883),
            "admin": discord.PartialEmoji(name="admin", id=1555957513701167145),
            "main_admin": discord.PartialEmoji(name="mainadmin", id=1555957596899246193)
        }
        return emojis.get(category, discord.PartialEmoji(name="help", id=1555957576997277857))
   
    def update_embed(self):
        """Update the embed based on current category and user permissions"""
        category_data = self.command_categories[self.current_category]
        # Create embed with category-specific styling
        colors = {
            "user": 0x3498db, # Blue
            "vps": 0x2ecc71, # Green
            "ports": 0xe74c3c, # Red
            "system": 0xf39c12, # Orange
            "bot": 0x9b59b6, # Purple
            "nodes": 0x1abc9c, # Teal
            "admin": 0xe67e22, # Carrot
            "main_admin": 0xf1c40f # Yellow
        }
        color = colors.get(self.current_category, 0x1a1a1a)
       
        title = f"<:help:1555957576997277857> {BOT_NAME} Command Help - {category_data['name']}"
        description = f"**{category_data['name']}**\nUse the dropdown below to switch categories."
       
        # Add helpful tips based on category
        tips = {
            "user": "Tip: Use `{PREFIX}myvps` to see all your VPS and `{PREFIX}manage` to control them.",
            "vps": "Tip: Snapshots are useful before making major changes to your VPS.",
            "ports": "Tip: Port forwards work for both TCP and UDP protocols.",
            "system": "Tip: Set thresholds to monitor resource usage across nodes.",
            "nodes": "Tip: Use `!node list` to see all available nodes and their status.",
            "admin": "Tip: Always check `!userinfo @user` before modifying VPS.",
            "main_admin": "Tip: Be careful when adding/removing admin privileges."
        }
       
        if self.current_category in tips:
            description += f"\n\n<:arrow_right:1555957522999808101> {tips[self.current_category]}"
       
        self.embed = create_embed(title, description, color)
       
        # Add commands to embed
        commands_text = "\n".join([f"**{cmd}** - {desc}" for cmd, desc in category_data["commands"]])
        add_field(self.embed, "<:arrow_right:1555957522999808101> Commands", commands_text, False)
       
        # Add appropriate footer based on category
        footers = {
            "user": f"{BOT_NAME} VPS Manager • User Commands • Need help? Contact admin",
            "vps": f"{BOT_NAME} VPS Manager • VPS Management • Snapshots • Cloning",
            "ports": f"{BOT_NAME} VPS Manager • Port Forwarding • TCP/UDP Support",
            "system": f"{BOT_NAME} VPS Manager • System Monitoring • Resource Management",
            "nodes": f"{BOT_NAME} VPS Manager • Multi-Node Management • Distributed Infrastructure",
            "bot": f"{BOT_NAME} VPS Manager • Bot Control • Status Management",
            "admin": f"{BOT_NAME} VPS Manager • Admin Panel • Restricted Access",
            "main_admin": f"{BOT_NAME} VPS Manager • Main Admin • Full System Control"
        }
       
        self.embed.set_footer(text=footers.get(self.current_category, f"{BOT_NAME} VPS Manager"))


@bot.command(name='help')
async def show_help(ctx):
    """Display the interactive help menu"""
    view = HelpView(ctx)
    await ctx.send(embed=view.embed, view=view)


# Command aliases for typos and convenience
@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction", f"Did you mean `{PREFIX}manage`? Use the correct command."))


@bot.command(name='commands')
async def commands_alias(ctx):
    """Alias for help command"""
    await show_help(ctx)


@bot.command(name='stats')
async def stats_alias(ctx):
    if str(ctx.author.id) in main_admin_ids or str(ctx.author.id) in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "This command requires admin privileges."))


@bot.command(name='info')
async def info_alias(ctx, user: discord.Member = None):
    if str(ctx.author.id) in main_admin_ids or str(ctx.author.id) in admin_data.get("admins", []):
        if user:
            await user_info(ctx, user)
        else:
            await ctx.send(embed=create_error_embed("Usage", f"Please specify a user: `{PREFIX}info @user`"))
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "This command requires admin privileges."))

# ---------------------------------------------------------------------------
# ZylexNode automatic off-host metadata backups
# IMPORTANT: Discord is not suitable for large VPS disk images. This subsystem
# uploads a consistent SQLite DB + VPS inventory every 5 minutes and keeps local
# archives. It does NOT export guest/rootfs disk contents; use LXD `lxc export`
# to migrate complete VPS filesystems. Never treat Discord as the only backup.
# ---------------------------------------------------------------------------
import tempfile
import zipfile
from pathlib import Path

_backup_lock = asyncio.Lock()
_last_backup_at = None
_last_backup_error = None

def _sqlite_consistent_copy(destination: str) -> None:
    """Use SQLite's backup API so WAL databases are copied consistently."""
    db_path = DATABASE_PATH if os.path.exists(DATABASE_PATH) else os.path.abspath("vps.db")
    if not os.path.isfile(db_path) or os.path.getsize(db_path) < 4096:
        raise RuntimeError(
            f"Database at {db_path} is missing or suspiciously small; refusing to upload an empty/incomplete backup. "
            "Set DATABASE_PATH to the live bot database path."
        )
    source = sqlite3.connect(db_path, timeout=30)
    target = sqlite3.connect(destination)
    try:
        tables = source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if not tables:
            raise RuntimeError(f"Database {db_path} contains no application tables; refusing to upload it")
        source.backup(target)
        target.commit()
        copied_tables = target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if len(copied_tables) != len(tables):
            raise RuntimeError("Database backup table count mismatch")
    finally:
        target.close()
        source.close()

def _backup_inventory() -> dict:
    # Exclude plaintext root passwords and other login secrets from the inventory.
    inventory = []
    for owner_id, entries in vps_data.items():
        for item in entries:
            inventory.append({
                "owner_id": str(owner_id),
                "id": item.get("id"),
                "container_name": item.get("container_name"),
                "node_id": item.get("node_id", 1),
                "ram": item.get("ram"),
                "cpu": item.get("cpu"),
                "storage": item.get("storage"),
                "config": item.get("config"),
                "os_version": item.get("os_version", "ubuntu:22.04"),
                "status": item.get("status", "unknown"),
                "suspended": bool(item.get("suspended", False)),
                "created_at": item.get("created_at"),
            })
    return {
        "product": "ZylexNode™",
        "developer": "rehan.404",
        "created_at": datetime.now().astimezone().isoformat(),
        "backup_type": "metadata-only; does not include guest filesystem contents",
        "vps_count": len(inventory),
        "vps": inventory,
        "nodes": [
            {k: n.get(k) for k in ("id", "name", "location", "total_vps", "is_local")}
            for n in get_nodes()
        ],
    }

def _make_metadata_archive() -> str:
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = os.path.join(BACKUP_DIR, f"zylexnode_metadata_{stamp}.zip")
    with tempfile.TemporaryDirectory(prefix="zylex-backup-") as tmp:
        db_copy = os.path.join(tmp, "vps.db")
        _sqlite_consistent_copy(db_copy)
        inventory_path = os.path.join(tmp, "inventory.json")
        with open(inventory_path, "w", encoding="utf-8") as f:
            json.dump(_backup_inventory(), f, indent=2, ensure_ascii=False)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(db_copy, "vps.db")
            zf.write(inventory_path, "inventory.json")
    # Verify archive structure and SQLite integrity before treating it as a valid backup.
    try:
        with zipfile.ZipFile(archive_path, "r") as check_zip:
            bad_member = check_zip.testzip()
            if bad_member:
                raise RuntimeError(f"Backup ZIP integrity check failed at {bad_member}")
            if not {"vps.db", "inventory.json"}.issubset(set(check_zip.namelist())):
                raise RuntimeError("Backup archive is missing required files")
            db_bytes = check_zip.read("vps.db")
        with tempfile.NamedTemporaryFile(prefix="zylex-db-check-", suffix=".db", delete=False) as db_tmp:
            db_tmp.write(db_bytes)
            check_db_path = db_tmp.name
        try:
            check_db = sqlite3.connect(check_db_path)
            integrity = check_db.execute("PRAGMA quick_check").fetchone()
            check_db.close()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
        finally:
            try:
                os.unlink(check_db_path)
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(archive_path)
        except OSError:
            pass
        raise
    os.chmod(archive_path, 0o600)
    # Retain only the newest configured number of archives.
    archives = sorted(Path(BACKUP_DIR).glob("zylexnode_metadata_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in archives[BACKUP_RETENTION:]:
        try:
            old.unlink()
        except OSError:
            logger.exception("Could not prune old backup %s", old)
    return archive_path

async def _perform_metadata_backup(upload_to_discord: bool = True) -> str:
    global _last_backup_at, _last_backup_error
    async with _backup_lock:
        archive = await asyncio.to_thread(_make_metadata_archive)
        size_bytes = os.path.getsize(archive)
        size_label = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.2f} MB"
        backup_channel_id = _env_int("BACKUP_CHANNEL_ID", int(get_setting("backup_channel_id", str(BACKUP_CHANNEL_ID))))
        channel = bot.get_channel(backup_channel_id) if upload_to_discord else None
        if upload_to_discord and channel is None:
            try:
                channel = await bot.fetch_channel(backup_channel_id)
            except Exception as exc:
                raise RuntimeError(f"Cannot access backup channel {backup_channel_id}; check channel ID and bot permissions: {exc}") from exc
        if channel is not None:
            channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if channel_guild_id is not None and int(channel_guild_id) != BACKUP_GUILD_ID:
                raise RuntimeError(
                    f"Backup channel {backup_channel_id} belongs to guild {channel_guild_id}, "
                    f"but configured guild is {BACKUP_GUILD_ID}. Check BACKUP_CHANNEL_ID/BACKUP_GUILD_ID."
                )
            max_bytes = BACKUP_MAX_UPLOAD_MB * 1024 * 1024
            if os.path.getsize(archive) > max_bytes:
                raise RuntimeError(
                    f"Metadata archive is {size_label}, above configured upload cap "
                    f"{BACKUP_MAX_UPLOAD_MB} MB. Local archive retained at {archive}."
                )
            await channel.send(
                content=(
                    f"🛡️ **ZylexNode™ metadata backup** · `{datetime.now().astimezone().isoformat(timespec='seconds')}`\n"
                    f"VPS inventory + consistent SQLite database. **Not a full guest-disk backup.**\n"
                    f"Archive: `{os.path.basename(archive)}` · {size_label}\n"
                    f"Integrity: ZIP verified + SQLite quick_check passed"
                ),
                file=discord.File(archive, filename=os.path.basename(archive)),
                allowed_mentions=discord.AllowedMentions.none()
            )
        _last_backup_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _last_backup_error = None
        logger.info("ZylexNode metadata backup completed: %s (%s)", archive, size_label)
        return archive

@tasks.loop(seconds=300)
async def automatic_metadata_backup():
    try:
        await _perform_metadata_backup(upload_to_discord=True)
    except Exception as exc:
        global _last_backup_error
        _last_backup_error = str(exc)[:1000]
        logger.exception("Automatic metadata backup failed")

@automatic_metadata_backup.before_loop
async def before_automatic_metadata_backup():
    await bot.wait_until_ready()

def _configured_log_channel_id() -> int:
    try:
        return int(get_setting("log_channel_id", str(DEFAULT_LOG_CHANNEL_ID)))
    except (TypeError, ValueError):
        return DEFAULT_LOG_CHANNEL_ID

async def _send_log_message(content: str, *, embed=None):
    """Send operations/health logs to the configured channel, without crashing the bot."""
    channel_id = _configured_log_channel_id()
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(content=content[:1900] if content else None, embed=embed,
                           allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception as exc:
        logger.warning("Could not send to configured log channel %s: %s", channel_id, exc)
        return False

@bot.command(name="set-log-channel")
@is_admin()
async def set_log_channel(ctx, channel: discord.TextChannel = None):
    """Set operations, overload and health-check log channel. Defaults to current channel."""
    channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if channel is None:
        await ctx.send(embed=create_error_embed("Invalid Channel", "Mention a text channel: `1set-log-channel #logs`"))
        return
    if ctx.guild and channel.guild.id != ctx.guild.id:
        await ctx.send(embed=create_error_embed("Invalid Channel", "Choose a channel in this server."))
        return
    set_setting("log_channel_id", str(channel.id))
    await ctx.send(embed=create_success_embed("Log Channel Updated", f"Health checks, overload alerts and diagnostic reports will be sent to {channel.mention}."))
    await _send_log_message(f"🛠️ ZylexNode™ log channel configured by {ctx.author} (`{ctx.author.id}`).")

@bot.command(name="set-backup-channel")
@is_admin()
async def set_backup_channel(ctx, channel: discord.TextChannel = None):
    """Set metadata/full-export backup destination, persisted in SQLite."""
    channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if channel is None:
        await ctx.send(embed=create_error_embed("Invalid Channel", "Mention a text channel: `1set-backup-channel #backups`"))
        return
    if ctx.guild and channel.guild.id != ctx.guild.id:
        await ctx.send(embed=create_error_embed("Invalid Channel", "Choose a channel in this server."))
        return
    set_setting("backup_channel_id", str(channel.id))
    await ctx.send(embed=create_success_embed("Backup Channel Updated", f"Automatic metadata backups and manual exports will use {channel.mention}."))
    await _send_log_message(f"🛡️ ZylexNode™ backup destination changed by {ctx.author} to <#{channel.id}>.")

@bot.command(name="host-resources", aliases=["resource-overview", "host-resource-overview"])
@is_admin()
async def host_resources(ctx, node_id: int = 1):
    """Show actual host CPU/RAM/disk capacity and database-tracked VPS allocations."""
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID `{node_id}` does not exist."))
        return

    # Allocation figures come from the bot's VPS inventory and are explicitly labelled
    # as allocated/configured, not measured real-time guest usage.
    allocated_cpu = allocated_ram = allocated_disk = 0.0
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT cpu, ram, storage FROM vps WHERE node_id = ?", (node_id,))
        rows = cur.fetchall()
        for row in rows:
            try: allocated_cpu += float(str(row["cpu"]).lower().replace("cores", "").replace("core", "").strip())
            except (TypeError, ValueError): pass
            try: allocated_ram += float(str(row["ram"]).lower().replace("gb", "").strip())
            except (TypeError, ValueError): pass
            try: allocated_disk += float(str(row["storage"]).lower().replace("gb", "").strip())
            except (TypeError, ValueError): pass
    finally:
        conn.close()

    embed = discord.Embed(
        title="<:stats:1555957645364564068> Host Resource Overview",
        description=f"Current resource capacity for **{node.get('name', f'Node {node_id}')}** · ID `{node_id}`",
        color=0x19B5FE,
        timestamp=datetime.now().astimezone(),
    )
    if node.get("is_local"):
        total_cpu = float(os.cpu_count() or 0)
        try:
            mem = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    key, value = line.split(":", 1)
                    mem[key] = int(value.strip().split()[0])  # kB
            total_ram = mem.get("MemTotal", 0) / 1024 / 1024
            available_ram = mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024 / 1024
            used_ram = max(0.0, total_ram - available_ram)
        except (OSError, ValueError, IndexError):
            total_ram = used_ram = 0.0
        try:
            disk = shutil.disk_usage("/")
            total_disk = disk.total / (1024 ** 3)
            used_disk = disk.used / (1024 ** 3)
            free_disk = disk.free / (1024 ** 3)
        except OSError:
            total_disk = used_disk = free_disk = 0.0

        embed.add_field(
            name="<:cpu:1555957536425771058> CPU",
            value=(f"**Total:** {total_cpu:.0f} cores\n**Allocated to VPS:** {allocated_cpu:g} cores\n"
                   f"**Available (unallocated):** {max(0.0, total_cpu-allocated_cpu):g} cores\n"
                   f"**Host usage:** {get_host_cpu_usage():.1f}%"), inline=False)
        embed.add_field(
            name="<:ram:1555957622358810717> RAM",
            value=(f"**Total:** {total_ram:.1f} GB\n**Allocated to VPS:** {allocated_ram:g} GB\n"
                   f"**Available (unallocated):** {max(0.0, total_ram-allocated_ram):.1f} GB\n"
                   f"**Host used:** {used_ram:.1f} GB ({(used_ram/total_ram*100 if total_ram else 0):.1f}%)"), inline=False)
        embed.add_field(
            name="<:disk:1555957549486964798> Storage",
            value=(f"**Total:** {total_disk:.1f} GB\n**Allocated to VPS:** {allocated_disk:g} GB\n"
                   f"**Available (unallocated):** {max(0.0, total_disk-allocated_disk):.1f} GB\n"
                   f"**Host used:** {used_disk:.1f} GB · **Free:** {free_disk:.1f} GB"), inline=False)
    else:
        # Remote API currently reports usage percentages, not total capacities. Do not
        # fabricate core/GB totals if the node API has not supplied them.
        stats = await get_host_stats(node_id)
        embed.add_field(name="<:cpu:1555957536425771058> CPU", value=f"**Allocated to VPS:** {allocated_cpu:g} cores\n**Host usage:** {float(stats.get('cpu', 0) or 0):.1f}%\n**Total capacity:** Not provided by node API", inline=False)
        embed.add_field(name="<:ram:1555957622358810717> RAM", value=f"**Allocated to VPS:** {allocated_ram:g} GB\n**Host usage:** {float(stats.get('ram', 0) or 0):.1f}%\n**Total capacity:** Not provided by node API", inline=False)
        embed.add_field(name="<:disk:1555957549486964798> Storage", value=f"**Allocated to VPS:** {allocated_disk:g} GB\n**Host usage:** {str(stats.get('disk', 'Unknown'))}\n**Total capacity:** Not provided by node API", inline=False)
    embed.set_footer(text="Configured VPS allocations are inventory totals, not live guest consumption.")
    await ctx.send(embed=embed)


@bot.command(name="health-check")
@is_admin()
async def health_check_now(ctx):
    """Run a read-only host/node/LXD check now."""
    await ctx.send(embed=create_info_embed("Health Check Started", "Checking node load, RAM, registered VPS state and LXD connectivity. This check is read-only."))
    findings = []
    for node in get_nodes():
        try:
            stats = await get_host_stats(int(node["id"]))
            cpu = float(stats.get("cpu", 0) or 0)
            ram = float(stats.get("ram", 0) or 0)
            state = "OVERLOAD" if cpu >= CPU_THRESHOLD or ram >= RAM_THRESHOLD else "OK"
            findings.append(f"**{node['name']}** · CPU {cpu:.1f}% · RAM {ram:.1f}% · **{state}**")
        except Exception as exc:
            findings.append(f"**{node.get('name', 'node')}** · CHECK FAILED: {str(exc)[:180]}")
    total = sum(len(items) for items in vps_data.values())
    running = sum(1 for items in vps_data.values() for v in items if str(v.get("status", "")).lower() == "running")
    embed = create_info_embed("ZylexNode™ Health Report", "\n".join(findings[:20]) or "No nodes registered.")
    add_field(embed, "Registered VPS", f"{total} total · {running} marked running", True)
    add_field(embed, "Thresholds", f"CPU {CPU_THRESHOLD}% · RAM {RAM_THRESHOLD}%", True)
    add_field(embed, "Automation", "Read-only diagnostics; no VPS is automatically restarted, stopped or resized.", False)
    await ctx.send(embed=embed)
    await _send_log_message(f"🩺 Manual health check requested by {ctx.author} (`{ctx.author.id}`).", embed=embed)

@tasks.loop(seconds=300)
async def automatic_vps_health_check():
    """Every 5 minutes, inspect host resource pressure and registered instance existence.

    Conservative auto-healing policy: report and diagnose; never restart, stop, resize,
    delete, or modify guest networking automatically because those actions can interrupt
    workloads or disconnect users. The log includes actionable AI-style recommendations.
    """
    try:
        alerts = []
        for node in get_nodes():
            node_id = int(node["id"])
            try:
                stats = await get_host_stats(node_id)
                cpu = float(stats.get("cpu", 0) or 0)
                ram = float(stats.get("ram", 0) or 0)
                if cpu >= CPU_THRESHOLD or ram >= RAM_THRESHOLD:
                    recommendations = []
                    if cpu >= CPU_THRESHOLD:
                        recommendations.append("CPU pressure: inspect `top`/`ps`, runaway processes and container CPU limits")
                    if ram >= RAM_THRESHOLD:
                        recommendations.append("RAM pressure: inspect `free -h`, `lxc info` and per-container memory usage; consider adding host RAM or adjusting limits after review")
                    alerts.append(f"⚠️ **OVERLOAD · {node['name']}** CPU `{cpu:.1f}%` / RAM `{ram:.1f}%`\nDiagnosis: " + "; ".join(recommendations))
                # Verify that registered instances are queryable, but do not change state.
                for owner_id, entries in list(vps_data.items()):
                    for vps in list(entries):
                        if int(vps.get("node_id", 1) or 1) != node_id:
                            continue
                        name = str(vps.get("container_name", ""))
                        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
                            continue
                        try:
                            info = await execute_lxc(name, f"info {name}", timeout=15, node_id=node_id)
                            if not info:
                                alerts.append(f"⚠️ **VPS CHECK · {name}** LXD returned no instance details; owner ID `{owner_id}`. Review manually.")
                        except Exception as exc:
                            alerts.append(f"⚠️ **VPS CHECK FAILED · {name}** node `{node['name']}` · {str(exc)[:240]}")
            except Exception as exc:
                alerts.append(f"⚠️ **NODE CHECK FAILED · {node.get('name', node_id)}** {str(exc)[:240]}")
        if alerts:
            # Keep Discord payloads bounded; send separate messages if required.
            for i in range(0, len(alerts), 5):
                body = "\n\n".join(alerts[i:i+5])
                await _send_log_message("🧠 **ZylexNode™ · 5-minute health monitor**\n" + body)
        logger.info("5-minute health monitor completed; alerts=%d", len(alerts))
    except Exception:
        logger.exception("Automatic VPS health check failed")

@automatic_vps_health_check.before_loop
async def before_automatic_vps_health_check():
    await bot.wait_until_ready()

@bot.command(name="backup-now")
@is_admin()
async def backup_now(ctx):
    """Create and upload a fresh ZylexNode metadata backup immediately."""
    await ctx.send(embed=create_info_embed("Backup Started", "Creating a consistent database + VPS inventory archive..."))
    try:
        path = await _perform_metadata_backup(upload_to_discord=True)
        await ctx.send(embed=create_success_embed(
            "Metadata Backup Complete",
            f"Uploaded `{os.path.basename(path)}` to the configured backup channel.\n"
            "**Note:** VPS guest disk contents are not included."
        ))
    except Exception as exc:
        logger.exception("Manual backup failed")
        await ctx.send(embed=create_error_embed("Backup Failed", str(exc)[:3500]))

@bot.command(name="backup-vps")
@is_admin()
async def backup_vps(ctx, vps_name: str):
    """Export one complete local LXD instance; large exports remain on disk."""
    owner_id, record = _find_vps_record(vps_name)
    if not record:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS named `{vps_name}` is registered."))
        return
    node_id = int(record.get("node_id", 1))
    node = get_node(node_id)
    if not node or not node.get("is_local"):
        await ctx.send(embed=create_error_embed(
            "Remote Node Export Not Available",
            "This command can export only instances on the bot's local LXD node. Remote nodes need a secure file-transfer/export endpoint; no remote export is claimed."
        ))
        return
    os.makedirs(os.path.join(BACKUP_DIR, "exports"), mode=0o700, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", vps_name)[:80]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_path = os.path.join(BACKUP_DIR, "exports", f"{safe_name}_{stamp}.tar.gz")
    await ctx.send(embed=create_info_embed(
        "Full VPS Export Started",
        f"Exporting `{vps_name}` including its LXD instance data. This may take a long time and require free disk space. The VPS is not intentionally stopped by this command."
    ))
    try:
        await execute_lxc(vps_name, f"export {shlex.quote(vps_name)} {shlex.quote(export_path)}", timeout=3600, node_id=node_id)
        if not os.path.isfile(export_path) or os.path.getsize(export_path) <= 0:
            raise RuntimeError("LXD export command finished but the export file is missing or empty")
        # LXD export is a tar archive. Test readable archive structure before upload.
        import tarfile
        with tarfile.open(export_path, "r:*") as tf:
            if not tf.getmembers():
                raise RuntimeError("Export archive contains no members; refusing to mark it valid")
        size_bytes = os.path.getsize(export_path)
        size_text = f"{size_bytes / 1024 / 1024:.2f} MB" if size_bytes >= 1024 * 1024 else f"{size_bytes / 1024:.1f} KB"
        if size_bytes > BACKUP_MAX_UPLOAD_MB * 1024 * 1024:
            await ctx.send(embed=create_warning_embed(
                "Full Export Verified — Too Large for Discord",
                f"`{os.path.basename(export_path)}` ({size_text}) passed a tar readability check and is saved on the VPS host at `{export_path}`. It exceeds the configured Discord upload cap ({BACKUP_MAX_UPLOAD_MB} MB), so it was not uploaded. Copy it to separate secure off-host storage."
            ))
            return
        backup_channel_id = _env_int("BACKUP_CHANNEL_ID", int(get_setting("backup_channel_id", str(BACKUP_CHANNEL_ID))))
        channel = bot.get_channel(backup_channel_id) or await bot.fetch_channel(backup_channel_id)
        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild_id is not None and int(channel_guild_id) != BACKUP_GUILD_ID:
            raise RuntimeError(f"Backup channel {backup_channel_id} belongs to guild {channel_guild_id}, expected {BACKUP_GUILD_ID}")
        await channel.send(
            content=(f"🛡️ **ZylexNode™ full LXD export** · VPS `{vps_name}` · {size_text}\n"
                     "Tar archive readability check passed. Test restore before relying on this backup."),
            file=discord.File(export_path, filename=os.path.basename(export_path)),
            allowed_mentions=discord.AllowedMentions.none()
        )
        await ctx.send(embed=create_success_embed(
            "Full VPS Export Uploaded",
            f"`{os.path.basename(export_path)}` ({size_text}) was uploaded to the backup channel and retained locally at `{export_path}`. This is a manual export, not a full export every five minutes."
        ))
    except Exception as exc:
        logger.exception("Full LXD export failed for %s", vps_name)
        await ctx.send(embed=create_error_embed("Full VPS Export Failed", f"{str(exc)[:2500]}\nIf a partial file exists, inspect it before deleting it."))

@bot.command(name="backup-status")
@is_admin()
async def backup_status(ctx):
    desc = (
        f"**Guild ID:** `{BACKUP_GUILD_ID}`\n"
        f"**Channel ID:** `{get_setting('backup_channel_id', str(BACKUP_CHANNEL_ID))}`\n"
        f"**Database path:** `{DATABASE_PATH}`\n"
        f"**Interval:** every {BACKUP_INTERVAL_SECONDS // 60} minute(s) (minimum 5)\n"
        f"**Local directory:** `{BACKUP_DIR}`\n"
        f"**Retention:** {BACKUP_RETENTION} archives\n"
        f"**Last success:** {_last_backup_at or 'Not yet'}\n"
        f"**Last error:** {(_last_backup_error or 'None')[:900]}\n\n"
        "This backs up the SQLite database and VPS inventory only—not the guest operating-system files."
    )
    await ctx.send(embed=create_info_embed("Automatic Backup Status", desc))


# ---------------------------------------------------------------------------
# Safe AI-style VPS health assistant. Uses live metrics + rule-based analysis;
# it never executes arbitrary AI-generated shell commands or silently changes VPSes.
# ---------------------------------------------------------------------------
def _find_vps_record(container_name: str):
    for owner_id, records in vps_data.items():
        for record in records:
            if str(record.get("container_name", "")).lower() == container_name.lower():
                return str(owner_id), record
    return None, None

async def _collect_vps_diagnostics(container_name: str):
    owner_id, record = _find_vps_record(container_name)
    if not record:
        raise ValueError(f"VPS `{container_name}` was not found in the bot database.")
    node_id = int(record.get("node_id", 1))
    checks = {}
    commands_to_run = {
        "state": f"info {container_name}",
        "memory": f"exec {container_name} -- free -m",
        "disk": f"exec {container_name} -- df -h /",
        "uptime": f"exec {container_name} -- uptime",
        "failed_services": f"exec {container_name} -- bash -lc 'systemctl --failed --no-legend 2>/dev/null | head -10'",
        "recent_errors": f"exec {container_name} -- bash -lc 'journalctl -p err -n 12 --no-pager 2>/dev/null | tail -12'",
    }
    for key, command in commands_to_run.items():
        try:
            checks[key] = str(await execute_lxc(container_name, command, timeout=25, node_id=node_id))[:3500]
        except Exception as exc:
            checks[key] = f"Unavailable: {str(exc)[:350]}"
    advice = []
    memory = checks.get("memory", "")
    m = re.search(r"^Mem:\s+(\d+)\s+(\d+)\s+(\d+)", memory, re.M)
    if m:
        total, used = int(m.group(1)), int(m.group(2))
        if total and used / total >= 0.90:
            advice.append(f"High memory pressure: approximately {used * 100 // total}% used. Check top memory processes and consider a RAM increase only after confirming sustained demand.")
    disk = checks.get("disk", "")
    if re.search(r"\s(?:9[0-9]|100)%\s+/$", disk, re.M):
        advice.append("Root filesystem is at least 90% full. Identify large logs/cache/files before deleting anything; do not run automatic cleanup blindly.")
    failed = checks.get("failed_services", "")
    if failed.strip() and "Unavailable:" not in failed and "0 loaded units listed" not in failed:
        advice.append("One or more systemd services may be failed. Inspect the service names and their journal before restarting them.")
    errors = checks.get("recent_errors", "")
    if errors.strip() and "Unavailable:" not in errors and "-- No entries --" not in errors:
        advice.append("Recent error-level journal entries exist. Review the included log excerpt to distinguish harmless noise from a real outage.")
    if not advice:
        advice.append("No obvious high-memory or near-full-disk warning was detected from the available output. This is not a guarantee of health; verify network, application ports, backups and service-specific logs.")
    return record, checks, advice

@bot.command(name="ai-doctor")
async def ai_doctor(ctx, vps_name: str):
    """Diagnose a VPS and produce evidence-based recommendations; no changes are applied."""
    owner_id, record = _find_vps_record(vps_name)
    if not record:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS named `{vps_name}` is registered."))
        return
    is_admin_user = str(ctx.author.id) in main_admin_ids or str(ctx.author.id) in admin_data.get("admins", [])
    if not is_admin_user and str(ctx.author.id) != owner_id and str(ctx.author.id) not in record.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You can only diagnose your own VPS unless you are an admin."))
        return
    await ctx.send(embed=create_info_embed("ZylexNode AI Doctor", f"Collecting live health signals for `{vps_name}`. This is read-only; no settings or files will be changed."))
    try:
        record, checks, advice = await _collect_vps_diagnostics(vps_name)
        summary = "\n".join(f"• {item}" for item in advice)
        embed = create_embed(f"AI Health Report · {vps_name}", summary, color=0x00ccff)
        embed.add_field(name="VPS limits", value=f"RAM: `{record.get('ram', '?')}` · CPU: `{record.get('cpu', '?')}` · Disk: `{record.get('storage', '?')}`", inline=False)
        for key, label in (("memory", "Memory"), ("disk", "Disk"), ("uptime", "Uptime"), ("failed_services", "Failed services"), ("recent_errors", "Recent errors")):
            value = checks.get(key, "Unavailable")
            embed.add_field(name=label, value=f"```text\n{value[:900]}\n```", inline=False)
        embed.set_footer(text="Evidence-based diagnostics • read-only • validate before applying changes")
        await ctx.send(embed=embed)
    except Exception as exc:
        logger.exception("AI doctor failed for %s", vps_name)
        await ctx.send(embed=create_error_embed("Diagnosis Failed", str(exc)[:3000]))

@bot.command(name="ai-optimize")
async def ai_optimize(ctx, vps_name: str):
    """Suggest safe optimizations without modifying the VPS automatically."""
    owner_id, record = _find_vps_record(vps_name)
    if not record:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS named `{vps_name}` is registered."))
        return
    is_admin_user = str(ctx.author.id) in main_admin_ids or str(ctx.author.id) in admin_data.get("admins", [])
    if not is_admin_user and str(ctx.author.id) != owner_id and str(ctx.author.id) not in record.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You can only optimize your own VPS unless you are an admin."))
        return
    try:
        record, checks, advice = await _collect_vps_diagnostics(vps_name)
        advice.extend([
            "Keep the OS security updates current and remove unused services only after checking dependencies.",
            "Use application-specific tuning (for example, JVM heap limits for Minecraft) rather than generic sysctl changes.",
            "Create and verify a restorable off-host backup before major package upgrades, disk cleanup or configuration changes.",
            "No changes were applied. Ask for a specific change and review its impact before authorizing execution.",
        ])
        embed = create_embed(f"Safe Optimization Plan · {vps_name}", "\n".join(f"• {item}" for item in advice), color=0x00ccff)
        embed.add_field(name="Current allocation", value=f"RAM `{record.get('ram', '?')}` · CPU `{record.get('cpu', '?')}` · Disk `{record.get('storage', '?')}`", inline=False)
        await ctx.send(embed=embed)
    except Exception as exc:
        logger.exception("AI optimization report failed for %s", vps_name)
        await ctx.send(embed=create_error_embed("Optimization Analysis Failed", str(exc)[:3000]))

# Run the bot
if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logger.critical("No Discord token found in DISCORD_TOKEN environment variable; refusing to start.")
        raise SystemExit(2)

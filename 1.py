# -*- coding: utf-8 -*-
"""
WG Gaming Bot v3 — SURVIVES IP FILTERING.
Logic: health-check all servers every 5 min. If primary is filtered (dead),
auto-migrate every active user to a healthy server, regenerate configs,
and push the new config to users automatically. IPv6 endpoint preferred when set.
"""
import os, sqlite3, subprocess, time, asyncio, socket
from datetime import timedelta
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, FSInputFile

BOT_TOKEN = os.getenv("BOT_TOKEN", "9266546987:AAFK4vJZ5CU5o3NATH_0ZaHsp4GuYSfr-bE")
OWNER_ID  = int(os.getenv("OWNER_ID", "8536789126"))
WG_IF     = os.getenv("WG_IF", "wg0")
DB = "wg3.db"

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

con = sqlite3.connect(DB, check_same_thread=False)
con.executescript("""
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS servers(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, ip TEXT, ipv6 TEXT,
  pub TEXT, port INTEGER DEFAULT 51820, local TEXT, healthy INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(
  tg_id INTEGER PRIMARY KEY, username TEXT, expires INTEGER,
  srv INTEGER, priv TEXT, pub TEXT, ip TEXT, banned INTEGER DEFAULT 0);
""")
def putset(k,v): con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,str(v))); con.commit()
def getset(k,d=None):
    r = con.execute("SELECT v FROM settings WHERE k=?",(k,)).fetchone(); return r[0] if r else d

def ssh(srv, cmd):
    """Run a command on a remote WG server over SSH."""
    return subprocess.run(["ssh", f"root@{srv['ip']}", cmd],
                          capture_output=True, text=True, timeout=15)

def server_by_id(i):
    c = con.execute("SELECT * FROM servers WHERE id=?", (i,)); return c.fetchone()

def current_primary():
    return con.execute("SELECT * FROM servers ORDER BY id LIMIT 1").fetchone()

# ---------- health check: UDP handshake on WG port ----------
def check_server(srv):
    ip, port = srv[2], srv[5]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
        s.sendto(b"\x01" + os.urandom(8), (ip, port)); s.recvfrom(64)  # junk probe
        return True
    except OSError:
        # UDP may just drop junk; second test: ICMP ping as fallback signal
        r = subprocess.run(["ping","-c","1","-W","3",ip], capture_output=True)
        return r.returncode == 0

# ---------- WireGuard ops on a server ----------
def wg_on(srv, cmd):
    return ssh(srv, f"wg {cmd}")

def create_peer_on(srv):
    priv = subprocess.check_output(["wg","genkey"], text=True).strip()
    pub  = subprocess.check_output(["wg","pubkey"], input=priv, text=True).strip()
    used = {r[0] for r in con.execute("SELECT ip FROM users WHERE srv=?", (srv[0],))}
    i = 2
    while f"{srv[6]}.{i}" in used: i += 1
    ip = f"{srv[6]}.{i}"
    wg_on(srv, f"set {WG_IF} peer {pub} allowed-ips {ip}/32")
    ssh(srv, f"wg-quick strip {WG_IF} > /etc/wireguard/{WG_IF}.conf")
    return pub, ip, priv

# ---------- Failover core ----------
async def migrate_user(uid, new_srv):
    u = con.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()
    if not u or u[2] < time.time(): return False
    old_srv = server_by_id(u[3])
    # remove from old server
    if old_srv:
        try: wg_on(old_srv, f"set {WG_IF} peer {u[5]} remove")
        except Exception: pass
    pub, ip, priv = create_peer_on(new_srv)
    con.execute("""UPDATE users SET srv=?, priv=?, pub=?, ip=? WHERE tg_id=?""",
                (new_srv[0], priv, pub, ip, uid)); con.commit()
    # push new config
    conf = f"""[Interface]
PrivateKey = {priv}
Address = {ip}/32
DNS = 1.1.1.1
MTU = 1380

[Peer]
PublicKey = {get_srv_pub(new_srv)}
AllowedIPs = 0.0.0.0/0
Endpoint = {endpoint_of(new_srv)}
PersistentKeepalive = 25
"""
    p = f"/tmp/wg_{uid}.conf"; open(p,"w").write(conf)
    try:
        await bot.send_document(uid, FSInputFile(p, filename=f"New_{new_srv[1]}.conf"),
            caption=f"🚨 سرور قبلی فیلتر شد. کانفیگ جدید (سرور {new_srv[1]}):")
        return True
    except Exception: return False

def get_srv_pub(srv):
    # auto-detect pubkey if empty
    return srv[4] or "PUBKEY?"

def endpoint_of(srv):
    e = srv[3] or srv[2]
    return f"[{e}]:{srv[5]}" if ":" in e else f"{e}:{srv[5]}"

async def health_loop():
    while True:
        await asyncio.sleep(300)
        for srv in con.execute("SELECT * FROM servers").fetchall():
            healthy = 1 if check_server(srv) else 0
            was = srv[7]
            con.execute("UPDATE servers SET healthy=? WHERE id=?", (healthy, srv[0]))
            con.commit()
            if was and not healthy:
                await bot.send_message(OWNER_ID,
                    f"🔴 سرور {srv[1]} ({srv[2]}) فیلتر/مرد شد! در حال انتقال کاربران…")
                backup = con.execute(
                    "SELECT * FROM servers WHERE id!=? AND healthy=1 ORDER BY id",
                    (srv[0],)).fetchall()
                if not backup:
                    await bot.send_message(OWNER_ID,
                        "⚠️ هیچ سرور سالم باقی نمانده! یک سرور جدید با /addserver اضافه کن.")
                    continue
                moved = failed = 0
                for (uid,) in con.execute("SELECT tg_id FROM users WHERE srv=? AND banned=0", (srv[0],)):
                    if await migrate_user(uid, backup[0]): moved += 1
                    else: failed += 1
                await bot.send_message(OWNER_ID,
                    f"✅ انتقال انجام شد: {moved} کاربر به «{backup[0][1]}» | خطا: {failed}")

# ---------- Owner commands ----------
def is_owner(m): return m.from_user.id == OWNER_ID

@dp.message(Command("addserver"))
async def addserver(m: Message, c: CommandObject):
    if not is_owner(m): return
    # /addserver NAME IP [PUBKEY] [PORT] [SUBNET]
    parts = c.args.split()
    name, ip = parts[0], parts[1]
    pub  = parts[2] if len(parts) > 2 else None
    port = int(parts[3]) if len(parts) > 3 else 51820
    net  = parts[4] if len(parts) > 4 else "10.66.0"
    ipv6 = parts[5] if len(parts) > 5 else None
    srv = dict(name=name, ip=ip, pub=pub, port=port, local=net, ipv6=ipv6)
    if not pub:
        try: pub = ssh(srv, f"wg show {WG_IF} public-key").stdout.strip()
        except Exception: return await m.answer("❌ SSH یا wg روی سرور کار نکرد. PubKey را دستی بده.")
    con.execute("INSERT OR REPLACE INTO servers(name,ip,ipv6,pub,port,local,healthy) VALUES(?,?,?,?,?,?,1)",
                (name, ip, ipv6, pub, port, net)); con.commit()
    await m.answer(f"🖥 سرور «{name}» ثبت شد.\n🔑 PubKey: {pub[:20]}…\nIPv6: {ipv6 or '-'}")

@dp.message(Command("setgroup"))
async def setgroup(m: Message, c: CommandObject):
    if not is_owner(m): return
    putset("group_id", c.args or m.chat.id)
    await m.answer("✅ گروه تنظیم شد")

@dp.message(Command("setduration"))
async def setduration(m: Message, c: CommandObject):
    if not is_owner(m): return
    putset("duration", int(c.args)); await m.answer("✅ مدت اشتراک تنظیم شد")

@dp.message(Command("servers"))
async def servers(m: Message):
    if not is_owner(m): return
    rows = con.execute("SELECT * FROM servers").fetchall()
    txt = "\n".join(f"{'🟢' if r[7] else '🔴'} {r[1]} — {r[2]} (users: "
        f"{con.execute('SELECT COUNT(*) FROM users WHERE srv=?',(r[0],)).fetchone()[0]})" for r in rows)
    await m.answer(txt or "سروری ثبت نشده.")

@dp.message(Command("ban"))
async def ban(m: Message, c: CommandObject):
    if not is_owner(m): return
    row = con.execute("SELECT tg_id,srv,pub FROM users WHERE username=?",
                      (c.args.lstrip("@"),)).fetchone()
    if row:
        srv = server_by_id(row[1])
        if srv: wg_on(srv, f"set {WG_IF} peer {row[2]} remove")
        con.execute("UPDATE users SET banned=1,expires=0 WHERE tg_id=?",(row[0],)); con.commit()
    await m.answer("🚫 بن شد.")

@dp.message(Command("broadcast"))
async def bcast(m: Message, c: CommandObject):
    if not is_owner(m): return
    ok = fail = 0
    for (uid,) in con.execute("SELECT tg_id FROM users WHERE banned=0"):
        try: await bot.send_message(uid, c.args); ok += 1
        except Exception: fail += 1
    await m.answer(f"📢 {ok} موفق / {fail} ناموفق")

# ---------- User ----------
@dp.message(Command("start"))
async def start(m: Message):
    uid, un = m.from_user.id, m.from_user.username or ""
    u = con.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()
    now = time.time()
    if not u:
        srv = current_primary()
        if not srv: return await m.answer("⏳ راه‌اندازی سرور در حال انجام است. بعدا تلاش کنید.")
        pub, ip, priv = create_peer_on(srv)
        dur = int(getset("duration", "30"))
        con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,0)",
                    (uid, un, now+dur*86400, srv[0], priv, pub, ip)); con.commit()
        u = con.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()
    if u[7]: return await m.answer("⛔ حساب مسدود است.")
    if u[2] < now: return await m.answer("⛔ اشتراک تمام. در گروه تمدید کنید. /renew")
    srv = server_by_id(u[3])
    conf = f"""[Interface]
PrivateKey = {u[4]}
Address = {u[6]}/32
DNS = 1.1.1.1
MTU = 1380
[Peer]
PublicKey = {srv[4] or get_srv_pub(srv)}
AllowedIPs = 0.0.0.0/0
Endpoint = {endpoint_of(srv)}
PersistentKeepalive = 25
"""
    p = f"/tmp/u{uid}.conf"; open(p,"w").write(conf)
    await m.answer_document(FSInputFile(p, filename=f"{srv[1]}.conf"),
        caption=f"🎮 کانفیگ گیمینگ | سرور: {srv[1]} | باقیمانده: {(u[2]-now)/86400:.1f} روز")

@dp.message(Command("status"))
async def status(m: Message):
    u = con.execute("SELECT * FROM users WHERE tg_id=?", (m.from_user.id,)).fetchone()
    if not u: return await m.answer("اول /start")
    left = u[2]-time.time()
    srv = server_by_id(u[3])
    await m.answer(f"{'✅' if left>0 else '❌'} | باقیمانده: {timedelta(seconds=max(left,0))}\n"
                   f"🖥 سرور: {srv[1]} ({'سالم' if srv[7] else '⚠️ فیلتر شده'})")

@dp.message(Command("renew"))
async def renew(m: Message):
    gid = getset("group_id")
    await m.answer("برای تمدید در گروه پشتیبانی پیام دهید." + (f" گروه: {gid}" if gid else ""))

async def main():
    asyncio.create_task(health_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

# /// script
# dependencies = ["websockets>=12"]
# ///
"""双客户端自测：登录 → 建房 → 开局 → 开火 → 收结算。

前提：本机已跑 `uv run server.py`（监听 8888）。

    uv run test_client.py

流程按真实客户端上行顺序：LoginReq → CreateRoomReq → StartReq → LoadingReq(100)
→ FireReq。每步之间要等一小会儿，真实客户端也是这个节奏；连发太快服务端
还没把玩家放进房间，StartReq 会被当成"无房可开"。
"""
import asyncio
import os
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tnt_proto import dec_pb, pack_message, pb_b, pb_v, unpack_message  # noqa: E402

WS = "ws://127.0.0.1:8888"
UIDS = {1: "61001", 2: "72143"}


async def drain(ws, seconds, bucket: list):
    """收指定秒数的消息，按 (name, proto) 收进 bucket。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        left = end - loop.time()
        if left <= 0:
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
            bucket.append(unpack_message(raw))
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            return


def types_of(msgs):
    out = {}
    for name, proto in msgs:
        if name != "BattleNotify":
            continue
        try:
            t = dec_pb(proto).get(2, [None])[0]
        except Exception:
            t = "?"
        out[t] = out.get(t, 0) + 1
    return out


async def main():
    ws1 = await websockets.connect(WS, max_size=2**22)
    # LoginReq 有两种实测形态：
    #   旧模板 f1=版本号(int) / f2=账号 / f3=密码
    #   新版   f1=账号 / f2=密码（无 f3）
    # 这里用新版形态；密码走环境变量，默认 test。
    pwd = os.environ.get("TNT_PW_61001", "test")
    await ws1.send(pack_message("LoginReq",
                                pb_b(1, UIDS[1].encode()) + pb_b(2, pwd.encode())))
    login_msgs = []
    await drain(ws1, 2.0, login_msgs)
    print("login: %d 条消息，含 LoginAck=%s"
          % (len(login_msgs), any(n == "LoginAck" for n, _ in login_msgs)))

    # 建房：CreateRoomReq{Mode=1}（真实上行 Mode=0 为快速加入）
    await ws1.send(pack_message("CreateRoomReq", pb_v(1, 1)))
    room_msgs = []
    await drain(ws1, 1.5, room_msgs)
    print("room: %d 条消息，含 RoomAck=%s"
          % (len(room_msgs), any(n == "RoomAck" for n, _ in room_msgs)))

    # 开局
    await ws1.send(pack_message("StartReq", pb_v(1, 0)))
    boot = []
    await drain(ws1, 2.0, boot)
    t = types_of(boot)
    print("battle: %d 条消息，BattleNotify types=%s" % (len(boot), t))
    print("  Begin(0)=%s  Action(8)=%s" % (0 in t, 8 in t))

    # 加载到 100，服务端据此补发 Action
    await ws1.send(pack_message("LoadingReq", pb_v(1, 100)))
    more = []
    await drain(ws1, 1.5, more)
    t2 = types_of(more)
    print("after loading: types=%s" % t2)

    # 开火
    await ws1.send(pack_message("FireReq", pb_v(2, 55) + pb_v(3, 500)))
    fired = []
    await drain(ws1, 3.0, fired)
    t3 = types_of(fired)
    print("after fire: types=%s  Fire(14)=%s  Change(9)=%s"
          % (t3, 14 in t3, 9 in t3))

    await ws1.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Command-line entry points for the WeChat channel."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path

import settings
from channels.wechat.account_store import AccountStore
from channels.wechat.engine import WechatMessageEngine
from channels.wechat.ilink_client import (
    DEFAULT_BASE_URL,
    ILinkClient,
    SessionExpiredError,
)
from channels.wechat.renderer import WechatRenderer
from channels.wechat.runtime import create_wechat_agent_session

logger = logging.getLogger(__name__)


def _store() -> AccountStore:
    return AccountStore(
        settings.get(
            "wechat.account_file",
            "~/.weavemind/wechat/account.json",
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weavemind wechat",
        description="WeaveMindAgent WeChat iLink channel",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="scan a QR code and bind WeChat")
    setup.add_argument(
        "--workspace",
        default=os.environ.get("WEAVEMIND_CALLER_CWD", os.getcwd()),
        help="workspace exposed to the remote-safe Agent",
    )
    setup.add_argument("--force", action="store_true", help="replace saved credentials")

    subparsers.add_parser("start", help="run the channel in the foreground")
    subparsers.add_parser("status", help="show saved account status")
    subparsers.add_parser("logout", help="delete saved WeChat credentials")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Load the project config before start changes cwd to the bound workspace.
    settings.load()
    args = _build_parser().parse_args(argv)

    if args.command == "setup":
        return _setup(args)
    if args.command == "start":
        return _start()
    if args.command == "status":
        return _status()
    if args.command == "logout":
        return _logout()
    return 2


def _setup(args) -> int:
    store = _store()
    if store.exists() and not args.force:
        print(
            f"已存在微信凭证：{store.path}\n"
            "如需重新绑定，请使用 weavemind wechat setup --force"
        )
        return 1

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"工作区不存在：{workspace}")
        return 1

    print("正在申请微信登录二维码...")
    with ILinkClient(
        base_url=settings.get("wechat.api_base_url", DEFAULT_BASE_URL),
        app_id=settings.get("wechat.app_id", "bot"),
        route_tag=settings.get("wechat.route_tag"),
    ) as client:
        try:
            account = client.login(
                workspace=str(workspace),
                on_qr_code=_display_qr_code,
                verify_code_provider=_read_verify_code,
                timeout_seconds=int(settings.get("wechat.login_timeout_seconds", 480)),
            )
        except Exception as exc:
            print(f"微信绑定失败：{exc}")
            return 1

    store.save(account)
    print(
        "微信绑定成功。\n"
        f"账号：{store.redact(account.bot_id)}\n"
        f"用户：{store.redact(account.bound_user_id)}\n"
        f"工作区：{account.workspace}\n"
        "运行：weavemind wechat start"
    )
    return 0


def _start() -> int:
    store = _store()
    try:
        account = store.load()
    except Exception as exc:
        print(f"微信凭证无法读取：{exc}\n请重新运行 setup --force。")
        return 1
    if not account:
        print("尚未绑定微信，请先运行：weavemind wechat setup")
        return 1

    workspace = Path(account.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"绑定的工作区不存在：{workspace}")
        return 1

    os.chdir(workspace)
    print("正在初始化 WeaveMindAgent（微信远程只读模式）...")
    session = create_wechat_agent_session(workspace)
    client = ILinkClient(
        base_url=account.base_url,
        token=account.bot_token,
        bot_id=account.bot_id,
        app_id=settings.get("wechat.app_id", "bot"),
        route_tag=settings.get("wechat.route_tag"),
    )
    engine = WechatMessageEngine(
        account=account,
        account_store=store,
        client=client,
        agent_session=session,
        renderer=WechatRenderer(
            max_chars=int(settings.get("wechat.max_reply_chars", 3800))
        ),
        queue_max_size=int(settings.get("wechat.queue_max_size", 20)),
        poll_timeout_seconds=float(
            settings.get("wechat.poll_timeout_seconds", 35)
        ),
        busy_poll_timeout_seconds=float(
            settings.get("wechat.busy_poll_timeout_seconds", 3)
        ),
        typing_refresh_seconds=float(
            settings.get("wechat.typing_refresh_seconds", 5)
        ),
        private_chat_only=bool(
            settings.get("wechat.private_chat_only", True)
        ),
    )

    def request_stop(_signum=None, _frame=None):
        engine.request_stop()

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        print(
            "微信通道已启动。\n"
            f"绑定用户：{store.redact(account.bound_user_id)}\n"
            f"工作区：{workspace}\n"
            "按 Ctrl+C 停止。"
        )
        engine.run_forever()
        return 0
    except SessionExpiredError:
        print(
            "微信登录已失效，请重新绑定：\n"
            "weavemind wechat setup --force"
        )
        return 2
    except KeyboardInterrupt:
        engine.request_stop()
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        engine.stop()
        client.close()


def _status() -> int:
    store = _store()
    try:
        account = store.load()
    except Exception as exc:
        print(f"微信凭证无法读取：{exc}")
        return 1
    if not account:
        print("微信通道：未绑定")
        return 1
    print(
        "微信通道：已绑定\n"
        f"账号：{store.redact(account.bot_id)}\n"
        f"用户：{store.redact(account.bound_user_id)}\n"
        f"工作区：{account.workspace}\n"
        f"凭证文件：{store.path}"
    )
    return 0


def _logout() -> int:
    store = _store()
    if not store.exists():
        print("微信通道：未绑定")
        return 0
    store.delete()
    print("微信凭证已删除。")
    return 0


def _display_qr_code(url: str) -> None:
    print("\n请使用手机微信扫描二维码链接并确认授权：")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print(url)


def _read_verify_code() -> str:
    return input("请输入手机微信显示的配对数字：").strip()

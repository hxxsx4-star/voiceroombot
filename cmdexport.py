"""각 봇이 자기 슬래시 명령어 목록을 공유 폴더에 내보낸다.

메인봇의 봇공지(사용법) 임베드가 이 파일들을 모아 자동으로 갱신하므로,
명령어를 추가/삭제하면 별도 작업 없이 안내가 따라 바뀐다.

모든 봇이 동일한 내용으로 가지고 있는 파일이다. (utils/logs.py 와 같은 방식)
"""
import json
import os

SHARED_DIR = os.environ.get("ARENA_SHARED_DIR", "/home/hxxsx4/shared_data")
CMD_DIR = os.path.join(SHARED_DIR, "commands")


def _walk(cmd, prefix=""):
    """그룹 명령어는 하위 명령까지 펼쳐서 기록한다."""
    import discord
    from discord import app_commands

    name = f"{prefix}{cmd.name}"
    if isinstance(cmd, app_commands.Group):
        for sub in cmd.commands:
            yield from _walk(sub, prefix=f"{name} ")
    else:
        yield {
            "name": name,
            "description": getattr(cmd, "description", "") or "",
            # 관리자 전용 여부 (기본 권한이 걸려 있으면 표시)
            "admin": bool(getattr(cmd, "default_permissions", None)),
        }


def export_commands(bot, bot_key: str, label: str) -> int:
    """봇의 명령어 목록을 {SHARED}/commands/{bot_key}.json 으로 저장한다."""
    try:
        cmds = []
        for c in bot.tree.get_commands():
            cmds.extend(_walk(c))
        cmds.sort(key=lambda x: x["name"])
        os.makedirs(CMD_DIR, exist_ok=True)
        path = os.path.join(CMD_DIR, f"{bot_key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"label": label, "commands": cmds}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return len(cmds)
    except Exception as e:
        print(f"🚨 [명령어 내보내기 실패] {bot_key}: {e}")
        return 0

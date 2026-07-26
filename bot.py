import discord
from discord import app_commands
import json
import os
import sqlite3
import time
import traceback
import urllib.request
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# 봇 오류/크래시 알림 채널 (운영자 전용)
ERROR_LOG_CH = 1530629960836841683

DB_PATH = os.environ.get('ARENA_DB_PATH', 'voice_settings.db')

# 방 이름 기본 양식 ({유저} 는 입장한 유저 이름으로 치환됨)
DEFAULT_NAME_TEMPLATE = "🔊 {유저}의 방"

# 데이터베이스 초기화 (설정값 저장용)
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# 여러 개의 생성채널을 지원하기 위해 generator_channel_id 를 PRIMARY KEY 로 사용한다.
# (한 서버에 여러 곳의 생성채널/카테고리 조합을 등록할 수 있음)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        generator_channel_id INTEGER PRIMARY KEY,
        guild_id INTEGER,
        category_id INTEGER,
        name_template TEXT
    )
''')

# 생성된 임시 채널 정보 저장 (봇 재시작에도 유지 / 방 주인 확인용)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS temp_channels (
        channel_id INTEGER PRIMARY KEY,
        guild_id INTEGER,
        owner_id INTEGER
    )
''')
conn.commit()


def migrate_schema_if_needed():
    """
    구버전 스키마(guild_id 가 PRIMARY KEY, 서버당 한 곳만 저장)에서
    신버전 스키마(generator_channel_id 가 PRIMARY KEY, 여러 곳 저장)로 마이그레이션한다.
    """
    cursor.execute("PRAGMA table_info(settings)")
    columns = cursor.fetchall()  # (cid, name, type, notnull, dflt_value, pk)
    pk_columns = [col[1] for col in columns if col[5] == 1]

    # 이미 신버전 스키마라면 아무것도 하지 않음
    if pk_columns == ['generator_channel_id']:
        return

    print('[마이그레이션] 구버전 설정 스키마를 여러 곳 지원 스키마로 변환합니다...')

    # 기존 데이터 백업
    cursor.execute('SELECT guild_id, generator_channel_id, category_id FROM settings')
    old_rows = cursor.fetchall()

    # 새 테이블로 교체
    cursor.execute('ALTER TABLE settings RENAME TO settings_old')
    cursor.execute('''
        CREATE TABLE settings (
            generator_channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            category_id INTEGER,
            name_template TEXT
        )
    ''')

    for guild_id, generator_channel_id, category_id in old_rows:
        if generator_channel_id is None:
            continue
        cursor.execute('''
            INSERT OR REPLACE INTO settings (generator_channel_id, guild_id, category_id, name_template)
            VALUES (?, ?, ?, ?)
        ''', (generator_channel_id, guild_id, category_id, None))

    cursor.execute('DROP TABLE settings_old')
    conn.commit()
    print('[마이그레이션] 완료되었습니다.')


def ensure_settings_columns():
    """구버전 settings 테이블에 name_template 컬럼이 없으면 추가한다."""
    cursor.execute("PRAGMA table_info(settings)")
    column_names = [col[1] for col in cursor.fetchall()]
    if 'name_template' not in column_names:
        cursor.execute("ALTER TABLE settings ADD COLUMN name_template TEXT")
        conn.commit()


migrate_schema_if_needed()
ensure_settings_columns()


# 봇 클래스 정의 (슬래시 명령어 동기화 포함)
class TempVCBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # 재시작 전에 올려둔 관리 패널의 버튼도 계속 동작하도록 뷰를 등록한다.
        self.add_view(RoomPanelView())
        # 슬래시 명령어를 디스코드에 동기화
        await self.tree.sync()

    async def on_error(self, event_method, *args, **kwargs):
        # 이벤트 핸들러 내부 예외는 봇을 죽이지 않고 여기로 넘어옵니다.
        # 콘솔에도 남기고, 운영자가 바로 볼 수 있게 디스코드에도 알립니다.
        err_text = traceback.format_exc()
        print(f"🚨 [이벤트 오류: {event_method}]\n{err_text}")
        channel = self.get_channel(ERROR_LOG_CH)
        if channel:
            try:
                await channel.send(
                    f"⚠️ **이벤트 처리 중 오류** (`{event_method}`)\n```py\n{err_text[-1800:]}\n```"
                )
            except Exception as e:
                print(f"🚨 [오류 로그 전송 실패] {e}")


def _report_crash_to_discord(token: str, error_text: str):
    """봇 프로세스 자체가 죽는 크래시는 게이트웨이 연결이 끊긴 상태라
    on_error 로 잡을 수 없습니다. 봇 토큰으로 REST API를 직접 호출해 알립니다."""
    if not ERROR_LOG_CH or not token:
        return
    try:
        content = f"🚨 **음성방봇 프로세스가 예기치 않게 종료되었습니다 (자동 재시작 예정)**\n```py\n{error_text[-1800:]}\n```"
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{ERROR_LOG_CH}/messages",
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                # Discord 는 기본 Python-urllib User-Agent 를 403 으로 막는다.
                # 이게 없으면 크래시 알림이 조용히 실패한다.
                "User-Agent": "DiscordBot (https://arenamatch.p-e.kr, 1.0)",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"🚨 [크래시 알림 전송 실패] {e}")


client = TempVCBot()


# ==========================================
# DB 헬퍼 함수
# ==========================================

# 특정 생성채널의 설정(카테고리, 방이름 양식)을 불러오는 함수
def get_generator_setting(generator_channel_id):
    cursor.execute(
        'SELECT category_id, name_template FROM settings WHERE generator_channel_id = ?',
        (generator_channel_id,)
    )
    return cursor.fetchone()

# 한 서버에 등록된 모든 생성채널 설정을 불러오는 함수
def get_guild_settings(guild_id):
    cursor.execute(
        'SELECT generator_channel_id, category_id, name_template FROM settings WHERE guild_id = ?',
        (guild_id,)
    )
    return cursor.fetchall()

# 임시 채널 등록/조회/삭제
def add_temp_channel(channel_id, guild_id, owner_id):
    cursor.execute(
        'INSERT OR REPLACE INTO temp_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)',
        (channel_id, guild_id, owner_id)
    )
    conn.commit()

def remove_temp_channel(channel_id):
    cursor.execute('DELETE FROM temp_channels WHERE channel_id = ?', (channel_id,))
    conn.commit()

def get_temp_channel_owner(channel_id):
    cursor.execute('SELECT owner_id FROM temp_channels WHERE channel_id = ?', (channel_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def set_temp_channel_owner(channel_id, owner_id):
    """방장을 바꾼다. (위임하거나, 방장이 나갔을 때 자동 승계)"""
    cursor.execute('UPDATE temp_channels SET owner_id = ? WHERE channel_id = ?',
                   (owner_id, channel_id))
    conn.commit()

def is_temp_channel(channel_id):
    cursor.execute('SELECT 1 FROM temp_channels WHERE channel_id = ?', (channel_id,))
    return cursor.fetchone() is not None


def build_channel_name(template, member):
    """방 이름 양식에 유저 이름을 채워 실제 채널 이름을 만든다."""
    if not template:
        template = DEFAULT_NAME_TEMPLATE
    name = (
        template
        .replace('{유저}', member.display_name)
        .replace('{user}', member.display_name)
        .replace('{user_name}', member.display_name)
        .replace('{username}', member.display_name)
        .replace('{닉네임}', member.display_name)
    )
    # 디스코드 채널 이름은 최대 100자
    return name[:100] if name else DEFAULT_NAME_TEMPLATE.replace('{유저}', member.display_name)


# ==========================================
# 1. 봇 설정 슬래시 명령어 (/음성세팅)
# ==========================================
@client.tree.command(name="음성세팅", description="임시 음성방 생성 채널/카테고리/방이름 양식을 설정합니다. (여러 곳 등록 가능)")
@app_commands.describe(
    생성채널="유저가 들어갔을 때 새로운 방이 생성될 음성 채널을 선택하세요.",
    카테고리="새로 만들어질 방들이 배치될 카테고리를 선택하세요.",
    방제목양식="(선택) 새 방 이름 양식. {유저}(또는 {user}) 자리에 입장한 유저 이름이 들어갑니다. 예: 🔊 {유저}의 게임방"
)
async def voice_setting(
    interaction: discord.Interaction,
    생성채널: discord.VoiceChannel,
    카테고리: discord.CategoryChannel,
    방제목양식: str = None,
):
    # 관리자 권한 확인
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild_id = interaction.guild_id

    # 이미 등록된 생성채널인지 확인 (수정인지 신규인지 + 기존 양식 보존용)
    existing = get_generator_setting(생성채널.id)
    is_update = existing is not None

    # 방제목양식을 안 넣었으면: 기존 값 유지, 없으면 기본값 사용
    if 방제목양식 is None:
        name_template = existing[1] if (existing and existing[1]) else DEFAULT_NAME_TEMPLATE
    else:
        name_template = 방제목양식

    # DB에 설정 저장 (같은 생성채널이면 덮어쓰기, 아니면 추가)
    cursor.execute('''
        INSERT OR REPLACE INTO settings (generator_channel_id, guild_id, category_id, name_template)
        VALUES (?, ?, ?, ?)
    ''', (생성채널.id, guild_id, 카테고리.id, name_template))
    conn.commit()

    total = len(get_guild_settings(guild_id))

    title = "⚙️ 음성 채널 설정 수정 완료" if is_update else "⚙️ 음성 채널 설정 추가 완료"
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="방 생성 통화방", value=생성채널.mention, inline=False)
    embed.add_field(name="생성될 카테고리", value=카테고리.name, inline=False)
    embed.add_field(name="방 이름 양식", value=f"`{name_template}`", inline=False)
    embed.set_footer(text=f"지정된 통화방에 입장하면 자동으로 새 방이 생성됩니다. (현재 등록된 생성채널: {total}개)")

    await interaction.response.send_message(embed=embed)


# ==========================================
# 1-2. 등록된 생성채널 목록 보기 (/음성세팅목록)
# ==========================================
@client.tree.command(name="음성세팅목록", description="현재 등록된 임시 음성방 생성채널 목록을 확인합니다.")
async def voice_setting_list(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild_id)

    if not settings:
        await interaction.response.send_message("아직 등록된 생성채널이 없습니다. `/음성세팅` 으로 추가해 주세요.", ephemeral=True)
        return

    embed = discord.Embed(title="📋 등록된 음성방 생성채널 목록", color=discord.Color.blurple())
    for generator_channel_id, category_id, name_template in settings:
        channel = interaction.guild.get_channel(generator_channel_id)
        category = interaction.guild.get_channel(category_id)
        channel_text = channel.mention if channel else f"(삭제된 채널: {generator_channel_id})"
        category_text = category.name if category else f"(삭제된 카테고리: {category_id})"
        template_text = name_template if name_template else DEFAULT_NAME_TEMPLATE
        embed.add_field(
            name=channel_text,
            value=f"→ 카테고리: {category_text}\n→ 방 이름 양식: `{template_text}`",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# 1-3. 생성채널 설정 제거 (/음성세팅제거)
# ==========================================
@client.tree.command(name="음성세팅제거", description="등록된 임시 음성방 생성채널 설정을 제거합니다.")
@app_commands.describe(생성채널="설정을 제거할 생성채널을 선택하세요.")
async def voice_setting_remove(interaction: discord.Interaction, 생성채널: discord.VoiceChannel):
    # 관리자 권한 확인
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if get_generator_setting(생성채널.id) is None:
        await interaction.response.send_message("❌ 해당 채널은 생성채널로 등록되어 있지 않습니다.", ephemeral=True)
        return

    cursor.execute('DELETE FROM settings WHERE generator_channel_id = ?', (생성채널.id,))
    conn.commit()

    await interaction.response.send_message(f"🗑️ {생성채널.mention} 생성채널 설정을 제거했습니다.", ephemeral=True)


# ==========================================
# 1-4. 방 제목 변경 (/방제목) - 일반 유저도 사용 가능
# ==========================================
@client.tree.command(name="방제목", description="지금 들어가 있는 임시 음성방의 제목을 변경합니다.")
@app_commands.describe(제목="새로운 방 제목")
async def rename_room(interaction: discord.Interaction, 제목: str):
    # 유저가 음성 채널에 들어가 있는지 확인
    voice_state = interaction.user.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("❌ 먼저 임시 음성방에 들어간 뒤 사용해 주세요.", ephemeral=True)
        return

    channel = voice_state.channel

    # 임시로 생성된 방인지 확인
    if not is_temp_channel(channel.id):
        await interaction.response.send_message("❌ 이 방은 봇이 만든 임시 방이 아니라 제목을 변경할 수 없습니다.", ephemeral=True)
        return

    # 방 주인 또는 관리자만 변경 가능
    owner_id = get_temp_channel_owner(channel.id)
    is_admin = interaction.user.guild_permissions.administrator
    if owner_id != interaction.user.id and not is_admin:
        await interaction.response.send_message("❌ 본인이 만든 방만 제목을 변경할 수 있습니다.", ephemeral=True)
        return

    새제목 = 제목.strip()
    if not 새제목:
        await interaction.response.send_message("❌ 제목이 비어 있습니다.", ephemeral=True)
        return
    새제목 = 새제목[:100]  # 디스코드 채널 이름 최대 100자

    # 채널 이름 변경은 레이트리밋(10분에 2회)이 있어 시간이 걸릴 수 있으므로 응답을 지연 처리
    await interaction.response.defer(ephemeral=True)
    try:
        await channel.edit(name=새제목)
        await interaction.followup.send(f"✅ 방 제목을 **{새제목}** (으)로 변경했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 채널 관리 권한이 없어 제목을 변경하지 못했습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 제목 변경 중 오류가 발생했습니다: {e}", ephemeral=True)


# ==========================================
# 2. 방 관리 패널 (방장 전용)
# ==========================================
# 패널은 생성된 음성방의 '채팅' 에 올라갑니다. 버튼을 누르면 그 메시지가 있는 채널이
# 곧 관리 대상 방이라, 뷰가 따로 상태를 들고 있지 않아도 됩니다.
# → timeout=None + 고정 custom_id 로 봇을 재시작해도 기존 패널이 계속 동작합니다.

MAX_USER_LIMIT = 99


def _member_in_channel(channel: discord.VoiceChannel, user_id: int):
    """음성방 안에 있는 멤버를 찾는다.

    members 인텐트(특권)가 없어 guild.get_member() 는 캐시 미스로 None 이 나올 수 있다.
    channel.members 는 음성 상태 이벤트로 채워지므로 이쪽이 확실하다.
    """
    return next((m for m in channel.members if m.id == user_id), None)


def _check_manager(interaction: discord.Interaction):
    """(관리 가능 여부, 실패 사유) 를 돌려준다. 대상 방은 패널이 올라온 채널."""
    channel = interaction.channel
    if not isinstance(channel, discord.VoiceChannel) or not is_temp_channel(channel.id):
        return None, "❌ 봇이 만든 임시 음성방에서만 사용할 수 있습니다."
    owner_id = get_temp_channel_owner(channel.id)
    if interaction.user.id != owner_id and not interaction.user.guild_permissions.administrator:
        owner = interaction.guild.get_member(owner_id) if owner_id else None
        who = f"({owner.display_name})" if owner else ""
        return None, f"❌ 방장{who}만 사용할 수 있습니다."
    return channel, ""


def build_panel_embed(channel: discord.VoiceChannel, owner: discord.Member | None):
    everyone = channel.overwrites_for(channel.guild.default_role)
    locked = everyone.connect is False
    hidden = everyone.view_channel is False
    limit = channel.user_limit

    embed = discord.Embed(
        title="🎛️ 방 관리",
        description="아래 버튼으로 이 방을 직접 관리할 수 있습니다. (방장 전용)",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="방장", value=owner.mention if owner else "알 수 없음", inline=True)
    embed.add_field(name="인원 제한", value=("무제한" if limit == 0 else f"{limit}명"), inline=True)
    embed.add_field(name="상태",
                    value=("🔒 잠김" if locked else "🔓 열림") + " · " +
                          ("🙈 비공개" if hidden else "👁️ 공개"),
                    inline=True)
    embed.set_footer(text="잠금: 새로 들어오는 것을 막음 · 비공개: 목록에서 숨김")
    return embed


async def refresh_panel(interaction: discord.Interaction, channel: discord.VoiceChannel):
    """패널 메시지의 임베드를 최신 상태로 갱신한다."""
    owner_id = get_temp_channel_owner(channel.id)
    owner = _member_in_channel(channel, owner_id) if owner_id else None
    try:
        await interaction.message.edit(embed=build_panel_embed(channel, owner))
    except (discord.HTTPException, AttributeError):
        pass


class RenameModal(discord.ui.Modal, title="방 이름 변경"):
    이름 = discord.ui.TextInput(label="새 방 이름", max_length=100, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        new_name = str(self.이름).strip()[:100]
        if not new_name:
            await interaction.response.send_message("❌ 이름이 비어 있습니다.", ephemeral=True)
            return
        # 채널 이름 변경은 10분에 2회 제한이라 오래 걸릴 수 있다.
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.edit(name=new_name)
            await interaction.followup.send(f"✅ 방 이름을 **{new_name}** 으로 변경했습니다.",
                                            ephemeral=True)
            await refresh_panel(interaction, channel)
        except discord.Forbidden:
            await interaction.followup.send("❌ 봇에게 채널 관리 권한이 없습니다.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ 변경 실패: {e}\n(이름 변경은 10분에 2회까지만 가능합니다)", ephemeral=True)


class LimitModal(discord.ui.Modal, title="인원 제한 변경"):
    인원 = discord.ui.TextInput(label="최대 인원 (0 = 무제한)", max_length=2, required=True,
                              placeholder="예: 5")

    async def on_submit(self, interaction: discord.Interaction):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        raw = str(self.인원).strip()
        if not raw.isdigit() or int(raw) > MAX_USER_LIMIT:
            await interaction.response.send_message(
                f"❌ 0 ~ {MAX_USER_LIMIT} 사이의 숫자를 입력하세요.", ephemeral=True)
            return
        limit = int(raw)
        try:
            await channel.edit(user_limit=limit)
        except discord.Forbidden:
            await interaction.response.send_message("❌ 봇에게 채널 관리 권한이 없습니다.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ 인원 제한을 **{'무제한' if limit == 0 else str(limit) + '명'}** 으로 설정했습니다.",
            ephemeral=True)
        await refresh_panel(interaction, channel)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="정말 삭제", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        if channel is None or not is_temp_channel(channel.id):
            await interaction.response.edit_message(content="❌ 대상 방을 찾을 수 없습니다.", view=None)
            return
        owner_id = get_temp_channel_owner(channel.id)
        if interaction.user.id != owner_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.edit_message(content="❌ 방장만 삭제할 수 있습니다.", view=None)
            return
        # 채널이 사라지면 응답할 곳도 없어지므로 먼저 답한 뒤 지운다.
        await interaction.response.edit_message(content="🗑️ 방을 삭제합니다...", view=None)
        try:
            await channel.delete(reason=f"방장({interaction.user}) 요청으로 삭제")
        except discord.HTTPException as e:
            print(f'방 삭제 실패: {e}')
        finally:
            remove_temp_channel(channel.id)


class RoomPanelView(discord.ui.View):
    """음성방 관리 패널. 상태를 들고 있지 않아 재시작 후에도 그대로 동작한다."""

    def __init__(self):
        super().__init__(timeout=None)

    # ── 1행: 기본 관리 ──
    @discord.ui.button(label="이름 변경", emoji="✏️", style=discord.ButtonStyle.secondary,
                       custom_id="vcpanel:rename", row=0)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="인원 제한", emoji="👥", style=discord.ButtonStyle.secondary,
                       custom_id="vcpanel:limit", row=0)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_modal(LimitModal())

    @discord.ui.button(label="잠금/해제", emoji="🔒", style=discord.ButtonStyle.secondary,
                       custom_id="vcpanel:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        everyone = channel.guild.default_role
        ow = channel.overwrites_for(everyone)
        now_locked = ow.connect is False
        ow.connect = None if now_locked else False
        try:
            await channel.set_permissions(everyone, overwrite=ow)
        except discord.Forbidden:
            await interaction.response.send_message("❌ 봇에게 권한 관리 권한이 없습니다.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(
            "🔓 방을 열었습니다. 누구나 입장할 수 있습니다." if now_locked
            else "🔒 방을 잠갔습니다. 허용한 사람만 입장할 수 있습니다.", ephemeral=True)
        await refresh_panel(interaction, channel)

    @discord.ui.button(label="공개/비공개", emoji="👁️", style=discord.ButtonStyle.secondary,
                       custom_id="vcpanel:hide", row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        everyone = channel.guild.default_role
        ow = channel.overwrites_for(everyone)
        now_hidden = ow.view_channel is False
        ow.view_channel = None if now_hidden else False
        try:
            await channel.set_permissions(everyone, overwrite=ow)
        except discord.Forbidden:
            await interaction.response.send_message("❌ 봇에게 권한 관리 권한이 없습니다.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(
            "👁️ 방을 공개했습니다." if now_hidden else "🙈 방을 숨겼습니다. 목록에 보이지 않습니다.",
            ephemeral=True)
        await refresh_panel(interaction, channel)

    @discord.ui.button(label="방 삭제", emoji="🗑️", style=discord.ButtonStyle.danger,
                       custom_id="vcpanel:delete", row=0)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(
            "정말 이 방을 삭제할까요? 안에 있는 사람도 모두 나가게 됩니다.",
            view=ConfirmDeleteView(), ephemeral=True)

    # ── 2행: 초대(입장 허용) ──
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="➕ 입장 허용할 사람 선택 (잠근 방에 초대)",
                       min_values=1, max_values=5, custom_id="vcpanel:invite", row=1)
    async def invite(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        done = []
        for user in select.values:
            try:
                await channel.set_permissions(user, connect=True, view_channel=True)
                done.append(user.display_name)
            except discord.Forbidden:
                await interaction.response.send_message("❌ 봇에게 권한 관리 권한이 없습니다.",
                                                        ephemeral=True)
                return
        await interaction.response.send_message(
            f"✅ {', '.join(done)} 님의 입장을 허용했습니다.", ephemeral=True)

    # ── 3행: 내보내기 ──
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="🚪 내보낼 사람 선택",
                       min_values=1, max_values=5, custom_id="vcpanel:kick", row=2)
    async def kick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        owner_id = get_temp_channel_owner(channel.id)
        kicked, skipped = [], []
        for user in select.values:
            member = _member_in_channel(channel, user.id)
            if member is None:
                skipped.append(user.display_name)
                continue
            if member.id == owner_id:
                skipped.append(f"{user.display_name}(방장)")
                continue
            try:
                await member.move_to(None, reason=f"방장({interaction.user}) 요청으로 내보냄")
                # 다시 못 들어오도록 입장 권한도 막는다.
                await channel.set_permissions(member, connect=False)
                kicked.append(member.display_name)
            except discord.Forbidden:
                skipped.append(f"{user.display_name}(권한부족)")
        msg = []
        if kicked:
            msg.append(f"🚪 {', '.join(kicked)} 님을 내보냈습니다. (재입장 차단)")
        if skipped:
            msg.append(f"⏭️ 건너뜀: {', '.join(skipped)}")
        await interaction.response.send_message("\n".join(msg) or "대상이 없습니다.", ephemeral=True)

    # ── 4행: 방장 위임 ──
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="👑 방장을 넘길 사람 선택",
                       min_values=1, max_values=1, custom_id="vcpanel:transfer", row=3)
    async def transfer(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        channel, err = _check_manager(interaction)
        if channel is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        target = select.values[0]
        member = _member_in_channel(channel, target.id)
        if member is None:
            await interaction.response.send_message(
                "❌ 이 방에 들어와 있는 사람에게만 방장을 넘길 수 있습니다.", ephemeral=True)
            return
        set_temp_channel_owner(channel.id, member.id)
        await interaction.response.send_message(
            f"👑 방장을 {member.mention} 님에게 넘겼습니다.", ephemeral=True)
        await refresh_panel(interaction, channel)


async def send_room_panel(channel: discord.VoiceChannel, owner: discord.Member):
    """새로 만들어진 방의 채팅에 관리 패널을 올린다."""
    try:
        await channel.send(content=f"{owner.mention} 님의 방입니다.",
                           embed=build_panel_embed(channel, owner), view=RoomPanelView())
    except discord.HTTPException as e:
        print(f'패널 전송 실패: {e}')


@client.tree.command(name="방관리", description="지금 들어가 있는 임시 음성방의 관리 패널을 엽니다. (방장 전용)")
async def room_panel(interaction: discord.Interaction):
    voice_state = interaction.user.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("❌ 먼저 임시 음성방에 들어간 뒤 사용해 주세요.",
                                                ephemeral=True)
        return
    channel = voice_state.channel
    if not is_temp_channel(channel.id):
        await interaction.response.send_message("❌ 이 방은 봇이 만든 임시 방이 아닙니다.",
                                                ephemeral=True)
        return
    owner_id = get_temp_channel_owner(channel.id)
    if interaction.user.id != owner_id and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 방장만 사용할 수 있습니다.", ephemeral=True)
        return
    owner = (_member_in_channel(channel, owner_id) or interaction.guild.get_member(owner_id)) \
        if owner_id else None
    # 패널은 방 채팅에 올려야 버튼이 대상 방을 알 수 있다.
    await channel.send(embed=build_panel_embed(channel, owner), view=RoomPanelView())
    await interaction.response.send_message("✅ 이 방의 채팅에 관리 패널을 올렸습니다.", ephemeral=True)


# ==========================================
# 3. 음성 감지 및 자동 방 생성/삭제 로직
# ==========================================
@client.event
async def on_voice_state_update(member, before, after):
    guild = member.guild

    # [A] 유저가 설정된 '방 생성 채널' 중 한 곳에 입장했을 때
    if after.channel is not None:
        setting = get_generator_setting(after.channel.id)
        if setting is not None:
            category_id, name_template = setting
            try:
                category = discord.utils.get(guild.categories, id=category_id)

                # 설정된 양식으로 새 음성 채널 생성
                new_channel = await guild.create_voice_channel(
                    name=build_channel_name(name_template, member),
                    category=category
                )

                add_temp_channel(new_channel.id, guild.id, member.id)
                await member.move_to(new_channel)
                await send_room_panel(new_channel, member)
                print(f'[생성] {member.display_name}님의 방이 만들어졌습니다.')

            except Exception as e:
                print(f'채널 생성 중 오류 발생: {e}')

    # [B] 유저가 음성 채널에서 퇴장했을 때
    if before.channel is not None and is_temp_channel(before.channel.id):
        remaining = before.channel.members
        if len(remaining) == 0:
            try:
                await before.channel.delete()
                print(f'[삭제] 빈 임시 채널이 삭제되었습니다.')
            except Exception as e:
                print(f'채널 삭제 중 오류 발생: {e}')
            finally:
                remove_temp_channel(before.channel.id)
        elif get_temp_channel_owner(before.channel.id) == member.id:
            # 방장이 나갔는데 사람이 남아 있으면 방이 관리 불능이 되므로,
            # 남아 있는 사람 중 한 명에게 방장을 넘긴다.
            new_owner = next((m for m in remaining if not m.bot), None)
            if new_owner is not None:
                set_temp_channel_owner(before.channel.id, new_owner.id)
                print(f'[방장 승계] {before.channel.name} → {new_owner.display_name}')
                try:
                    await before.channel.send(
                        f"👑 방장이 나가서 {new_owner.mention} 님이 새 방장이 되었습니다.\n"
                        "`/방관리` 로 관리 패널을 열 수 있습니다.")
                except discord.HTTPException:
                    pass


@client.event
async def on_ready():
    print(f'✅ {client.user} 봇이 준비되었습니다!')

    # 재시작 후 남아있는(비어있거나 이미 삭제된) 임시 채널 정리
    cursor.execute('SELECT channel_id FROM temp_channels')
    for (channel_id,) in cursor.fetchall():
        channel = client.get_channel(channel_id)
        if channel is None:
            remove_temp_channel(channel_id)  # 이미 사라진 채널
        elif len(channel.members) == 0:
            try:
                await channel.delete()
                print(f'[정리] 시작 시 빈 임시 채널을 삭제했습니다.')
            except Exception as e:
                print(f'시작 시 채널 정리 오류: {e}')
            finally:
                remove_temp_channel(channel_id)

# 봇 실행
# client 가 모듈 전역이라 in-process 재시작은 불가능합니다. 크래시를 알린 뒤
# 종료하면 컨테이너의 restart 정책(unless-stopped)이 프로세스를 다시 띄웁니다.
try:
    client.run(TOKEN)
except discord.LoginFailure:
    print("🚨 로그인 실패: 토큰(.env DISCORD_TOKEN)을 확인하세요.")
    raise
except Exception:
    err_text = traceback.format_exc()
    print(f"🚨 [봇 종료 - 예외 발생]\n{err_text}")
    _report_crash_to_discord(TOKEN, err_text)
    # 크래시 루프 시 재시작 폭주를 막기 위한 최소 대기
    time.sleep(5)
    raise

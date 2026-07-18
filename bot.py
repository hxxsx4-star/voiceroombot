import discord
from discord import app_commands
import os
import sqlite3
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# 방 이름 기본 양식 ({유저} 는 입장한 유저 이름으로 치환됨)
DEFAULT_NAME_TEMPLATE = "🔊 {유저}의 방"

# 데이터베이스 초기화 (설정값 저장용)
conn = sqlite3.connect('voice_settings.db')
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
        # 슬래시 명령어를 디스코드에 동기화
        await self.tree.sync()

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
# 2. 음성 감지 및 자동 방 생성/삭제 로직
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
                print(f'[생성] {member.display_name}님의 방이 만들어졌습니다.')

            except Exception as e:
                print(f'채널 생성 중 오류 발생: {e}')

    # [B] 유저가 음성 채널에서 퇴장했을 때 (빈 임시 채널 삭제)
    if before.channel is not None and is_temp_channel(before.channel.id):
        if len(before.channel.members) == 0:
            try:
                await before.channel.delete()
                print(f'[삭제] 빈 임시 채널이 삭제되었습니다.')
            except Exception as e:
                print(f'채널 삭제 중 오류 발생: {e}')
            finally:
                remove_temp_channel(before.channel.id)


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
client.run(TOKEN)

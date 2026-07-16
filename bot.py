import discord
from discord import app_commands
import os
import sqlite3
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# 데이터베이스 초기화 (설정값 저장용)
conn = sqlite3.connect('voice_settings.db')
cursor = conn.cursor()

# 여러 개의 생성채널을 지원하기 위해 generator_channel_id 를 PRIMARY KEY 로 사용한다.
# (한 서버에 여러 곳의 생성채널/카테고리 조합을 등록할 수 있음)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        generator_channel_id INTEGER PRIMARY KEY,
        guild_id INTEGER,
        category_id INTEGER
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
            category_id INTEGER
        )
    ''')

    for guild_id, generator_channel_id, category_id in old_rows:
        if generator_channel_id is None:
            continue
        cursor.execute('''
            INSERT OR REPLACE INTO settings (generator_channel_id, guild_id, category_id)
            VALUES (?, ?, ?)
        ''', (generator_channel_id, guild_id, category_id))

    cursor.execute('DROP TABLE settings_old')
    conn.commit()
    print('[마이그레이션] 완료되었습니다.')


migrate_schema_if_needed()


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

# 임시 채널 ID를 저장할 세트
temp_channels = set()

# 특정 생성채널의 설정(카테고리)을 불러오는 함수
def get_generator_setting(generator_channel_id):
    cursor.execute(
        'SELECT category_id FROM settings WHERE generator_channel_id = ?',
        (generator_channel_id,)
    )
    return cursor.fetchone()

# 한 서버에 등록된 모든 생성채널 설정을 불러오는 함수
def get_guild_settings(guild_id):
    cursor.execute(
        'SELECT generator_channel_id, category_id FROM settings WHERE guild_id = ?',
        (guild_id,)
    )
    return cursor.fetchall()


# ==========================================
# 1. 봇 설정 슬래시 명령어 (/음성세팅)
# ==========================================
@client.tree.command(name="음성세팅", description="임시 음성방 생성 전용 채널과 카테고리를 추가합니다. (여러 곳 등록 가능)")
@app_commands.describe(
    생성채널="유저가 들어갔을 때 새로운 방이 생성될 음성 채널을 선택하세요.",
    카테고리="새로 만들어질 방들이 배치될 카테고리를 선택하세요."
)
async def voice_setting(interaction: discord.Interaction, 생성채널: discord.VoiceChannel, 카테고리: discord.CategoryChannel):
    # 관리자 권한 확인
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild_id = interaction.guild_id

    # 이미 등록된 생성채널인지 확인 (수정인지 신규인지 안내용)
    is_update = get_generator_setting(생성채널.id) is not None

    # DB에 설정 저장 (같은 생성채널이면 덮어쓰기, 아니면 추가)
    cursor.execute('''
        INSERT OR REPLACE INTO settings (generator_channel_id, guild_id, category_id)
        VALUES (?, ?, ?)
    ''', (생성채널.id, guild_id, 카테고리.id))
    conn.commit()

    total = len(get_guild_settings(guild_id))

    title = "⚙️ 음성 채널 설정 수정 완료" if is_update else "⚙️ 음성 채널 설정 추가 완료"
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="방 생성 통화방", value=생성채널.mention, inline=False)
    embed.add_field(name="생성될 카테고리", value=카테고리.name, inline=False)
    embed.set_footer(text=f"이제 지정된 통화방에 입장하면 자동으로 새로운 방이 생성됩니다. (현재 등록된 생성채널: {total}개)")

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
    for generator_channel_id, category_id in settings:
        channel = interaction.guild.get_channel(generator_channel_id)
        category = interaction.guild.get_channel(category_id)
        channel_text = channel.mention if channel else f"(삭제된 채널: {generator_channel_id})"
        category_text = category.name if category else f"(삭제된 카테고리: {category_id})"
        embed.add_field(name=channel_text, value=f"→ 카테고리: {category_text}", inline=False)

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
# 2. 음성 감지 및 자동 방 생성/삭제 로직
# ==========================================
@client.event
async def on_voice_state_update(member, before, after):
    guild = member.guild

    # [A] 유저가 설정된 '방 생성 채널' 중 한 곳에 입장했을 때
    if after.channel is not None:
        setting = get_generator_setting(after.channel.id)
        if setting is not None:
            category_id = setting[0]
            try:
                category = discord.utils.get(guild.categories, id=category_id)

                # 새 음성 채널 생성
                new_channel = await guild.create_voice_channel(
                    name=f"🔊 {member.display_name}의 방",
                    category=category
                )

                temp_channels.add(new_channel.id)
                await member.move_to(new_channel)
                print(f'[생성] {member.display_name}님의 방이 만들어졌습니다.')

            except Exception as e:
                print(f'채널 생성 중 오류 발생: {e}')

    # [B] 유저가 음성 채널에서 퇴장했을 때 (빈 임시 채널 삭제)
    if before.channel is not None and before.channel.id in temp_channels:
        if len(before.channel.members) == 0:
            try:
                await before.channel.delete()
                temp_channels.remove(before.channel.id)
                print(f'[삭제] 빈 임시 채널이 삭제되었습니다.')
            except Exception as e:
                print(f'채널 삭제 중 오류 발생: {e}')


@client.event
async def on_ready():
    print(f'✅ {client.user} 봇이 준비되었습니다!')

# 봇 실행
client.run(TOKEN)

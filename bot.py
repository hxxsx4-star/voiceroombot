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
cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER PRIMARY KEY,
        generator_channel_id INTEGER,
        category_id INTEGER
    )
''')
conn.commit()

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

# 디스코드 설정값 불러오기 함수
def get_settings(guild_id):
    cursor.execute('SELECT generator_channel_id, category_id FROM settings WHERE guild_id = ?', (guild_id,))
    return cursor.fetchone()


# ==========================================
# 1. 봇 설정 슬래시 명령어 (/음성세팅)
# ==========================================
@client.tree.command(name="음성세팅", description="임시 음성방 생성 전용 채널과 카테고리를 설정합니다.")
@app_commands.describe(
    생성채널="유저가 들어갔을 때 새로운 방이 생성될 음성 채널을 선택하세요.",
    카테고리="새로 만들어질 방들이 배치될 카테고리의 ID를 입력하세요."
)
async def voice_setting(interaction: discord.Interaction, 생성채널: discord.VoiceChannel, 카테고리: discord.CategoryChannel):
    # 관리자 권한 확인
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    
    # DB에 설정 저장 (있으면 덮어쓰기)
    cursor.execute('''
        INSERT OR REPLACE INTO settings (guild_id, generator_channel_id, category_id)
        VALUES (?, ?, ?)
    ''', (guild_id, 생성채널.id, 카테고리.id))
    conn.commit()

    embed = discord.Embed(title="⚙️ 음성 채널 설정 완료", color=discord.Color.green())
    embed.add_field(name="방 생성 통화방", value=생성채널.mention, inline=False)
    embed.add_field(name="생성될 카테고리", value=카테고리.name, inline=False)
    embed.set_footer(text="이제 지정된 통화방에 입장하면 자동으로 새로운 방이 생성됩니다.")

    await interaction.response.send_message(embed=embed)


# ==========================================
# 2. 음성 감지 및 자동 방 생성/삭제 로직
# ==========================================
@client.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    settings = get_settings(guild.id)
    
    # 설정이 되어있지 않다면 작동하지 않음
    if not settings:
        return
        
    generator_channel_id, category_id = settings

    # [A] 유저가 설정된 '방 생성 채널'에 입장했을 때
    if after.channel is not None and after.channel.id == generator_channel_id:
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

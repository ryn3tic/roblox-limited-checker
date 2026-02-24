import discord
from discord import app_commands
import os

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")


@tree.command(name="profit", description="Calculate profit after Roblox tax")
@app_commands.describe(buy_price="Price you bought the item for",
                       sell_price="Current lowest sell price")
async def profit(interaction: discord.Interaction, buy_price: float, sell_price: float):

    net = sell_price * 0.7
    profit_value = net - buy_price
    roi = (profit_value / buy_price) * 100

    embed = discord.Embed(
        title="📈 Profit Calculator",
        color=discord.Color.green()
    )

    embed.add_field(name="Buy Price", value=f"{buy_price}", inline=True)
    embed.add_field(name="Sell Price", value=f"{sell_price}", inline=True)
    embed.add_field(name="After Tax (70%)", value=f"{net:.2f}", inline=False)
    embed.add_field(name="Profit", value=f"{profit_value:.2f}", inline=True)
    embed.add_field(name="ROI %", value=f"{roi:.2f}%", inline=True)

    await interaction.response.send_message(embed=embed)


client.run(TOKEN)

import os

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Bot ID: {bot.user.id}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    print("\n========== MESSAGE ==========")
    print("Content:", repr(message.content))

    print("\nAUTHOR")
    print("  Name:", message.author)
    print("  ID:", message.author.id)

    print("\nMENTIONS")
    print("  Raw:", message.mentions)

    for user in message.mentions:
        print("  Name:", user.name)
        print("  Display:", user.display_name)
        print("  ID:", user.id)

    print("\nREFERENCES")
    print("  Reference:", message.reference)

    if message.reference:
        print("  Referenced ID:", message.reference.message_id)

    print("\nREACTIONS ALREADY ON MESSAGE")
    for reaction in message.reactions:
        print("  Emoji:", reaction.emoji)
        print("  Count:", reaction.count)

    print("==============================\n")


@bot.event
async def on_reaction_add(reaction, user):
    print("\n========== REACTION ADD ==========")
    print("User:", user)
    print("User ID:", user.id)
    print("Emoji:", repr(reaction.emoji))
    print("Message ID:", reaction.message.id)
    print("Message:", repr(reaction.message.content))
    print("===================================\n")


@bot.event
async def on_reaction_remove(reaction, user):
    print("\n========== REACTION REMOVE ==========")
    print("User:", user)
    print("User ID:", user.id)
    print("Emoji:", repr(reaction.emoji))
    print("Message ID:", reaction.message.id)
    print("Message:", repr(reaction.message.content))
    print("======================================\n")


@bot.event
async def on_message_edit(before, after):
    if after.author == bot.user:
        return

    print("\n========== MESSAGE EDITED ==========")

    print("Message ID:", after.id)

    print("\nBEFORE")
    print("Content:", repr(before.content))

    print("\nAFTER")
    print("Content:", repr(after.content))

    print("\nAUTHOR")
    print("Name:", after.author)
    print("ID:", after.author.id)

    print("\nMENTIONS AFTER EDIT")
    for user in after.mentions:
        print("Name:", user.name)
        print("ID:", user.id)

    print("====================================\n")


bot.run(TOKEN)
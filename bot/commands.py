"""Discord slash commands for the betting bot."""

import asyncio
import discord
from datetime import datetime, timedelta
from discord import app_commands
from discord.ext import commands
import os
from typing import Optional

from utils import storage
from scraper.factory import scrape_any
from bot.views import MatchView, BetAmountModal


class BettingCommands(commands.Cog):
    """All betting-related slash commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.admin_ids = set()
        
        # Load admin IDs from environment
        admin_ids_str = os.getenv("ADMIN_USER_IDS", "")
        if admin_ids_str:
            self.admin_ids = set(admin_ids_str.split(","))
    
    def is_admin(self, user_id: str) -> bool:
        """Check if user is an admin."""
        return user_id in self.admin_ids
    
    @app_commands.command(name="match", description="View a match and place bets")
    @app_commands.describe(match_id="The match ID from cross.bet")
    async def match_command(self, interaction: discord.Interaction, match_id: str):
        """Display match information with betting buttons."""
        await interaction.response.defer(thinking=True)
        
        # Scrape match data
        match_data = await asyncio.to_thread(scrape_any, match_id)
        
        if not match_data:
            await interaction.followup.send(
                f"Could not find or parse match data for: {match_id}. Please ensure it is a valid ID or cross.bet URL.",
                ephemeral=True
            )
            return
        
        # Use the real match_id from the data in case a URL was passed
        real_match_id = match_data["match_id"]

        existing_match = storage.get_match(real_match_id)

        # If match already exists and haska deadline, keep the old deadline.
        if existing_match and "betting_closes_at" in existing_match:
            match_data["betting_closes_at"] = existing_match["betting_closes_at"]
        else:
            closes_at = datetime.now() + timedelta(minutes=2)
            match_data["betting_closes_at"] = closes_at.isoformat()
        
        # Save match to storage
        storage.save_match(real_match_id, match_data)
        
        # Check if match has ended (series complete - not just a map)
        if match_data["score_a"] >= 2 or match_data["score_b"] >= 2:
            await interaction.followup.send(
                f"This match has already ended! Final score: {match_data['team_a']} {match_data['score_a']} - {match_data['score_b']} {match_data['team_b']}",
                ephemeral=True
            )
            return
        
        # Check if user already bet on this map
        user_id = str(interaction.user.id)
        existing_bet = storage.get_user_bet_for_map(
            user_id, match_id, match_data["map_number"]
        )
        
        # Create embed
        embed = discord.Embed(
            title=f"{match_data['event']}",
            description=f"**{match_data['team_a']}** vs **{match_data['team_b']}**",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Current Map",
            value=f"Map {match_data['map_number']} - {match_data['current_map']}",
            inline=False
        )
        
        embed.add_field(
            name="Series Score",
            value=f"{match_data['score_a']} - {match_data['score_b']}",
            inline=True
        )
        
        if match_data["status"] == "live":
            embed.add_field(
                name="Round Score",
                value=f"{match_data['round_score_a']} - {match_data['round_score_b']}",
                inline=True
            )
        
        embed.add_field(
            name="Status",
            value=match_data["status"].upper(),
            inline=True
        )
        
        # Show info about series progress
        if match_data["score_a"] + match_data["score_b"] > 0:
            embed.add_field(
                name="Series Progress",
                value=f"First to 2 maps wins",
                inline=False
            )
        if "betting_closes_at" in match_data:
            closes_at_dt = datetime.fromisoformat(match_data["betting_closes_at"])
            unix_timestamp = int(closes_at_dt.timestamp())

            embed.add_field(
                name="⏳ Betting Closes",
                value= f"<t:{unix_timestamp}:R>",
                inline=False
            )
        
        if existing_bet:
            embed.add_field(
                name="Your Bet",
                value=f"You already bet **{existing_bet['amount']}** on **{existing_bet['team']}** at odds **{existing_bet['odds']}**",
                inline=False
            )
        
        # Create view with buttons (disable if already bet)
        if existing_bet:
            await interaction.followup.send(embed=embed)
        else:
            view = MatchView(
                match_id,
                match_data["team_a"],
                match_data["team_b"],
                match_data["odds_a"],
                match_data["odds_b"],
                match_data["map_number"]
            )
            message = await interaction.followup.send(embed=embed, view=view, wait=True)
            view.message = message
    
    @app_commands.command(name="bet", description="Place a bet on a match")
    @app_commands.describe(
        match_id="The match ID from cross.bet",
        team="Team to bet on",
        amount="Amount to bet (min 10)"
    )
    @app_commands.choices(team=[
        app_commands.Choice(name="Team A", value="a"),
        app_commands.Choice(name="Team B", value="b")
    ])
    async def bet_command(
        self,
        interaction: discord.Interaction,
        match_id: str,
        team: app_commands.Choice[str],
        amount: int
    ):
        """Place a bet using command instead of buttons."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Validate amount
        if amount < 10:
            await interaction.followup.send("Minimum bet is 10 tokens!", ephemeral=True)
            return
        
        # Get user
        user_id = str(interaction.user.id)
        user = storage.get_or_create_user(user_id)
        
        if user["balance"] < amount:
            await interaction.followup.send(
                f"Insufficient balance! You have {user['balance']} tokens.",
                ephemeral=True
            )
            return
        
        # Get match data
        match = storage.get_match(match_id)
        if not match:
            # Try to scrape (they probably passed a URL)
            fresh_match = await asyncio.to_thread(scrape_any, match_id)

            if fresh_match:
                real_match_id = fresh_match["match_id"]

                # Check if this match ALREADY exists under its real ID!
                existing_match = storage.get_match(real_match_id)

                if existing_match:
                    # It exists! Keep the old deadline.
                    match = existing_match
                    match_id = real_match_id
                else:
                    # It's actually a brand new match. Give it 2 minutes.
                    match = fresh_match
                    match_id = real_match_id
                    match["betting_closes_at"] = (datetime.now() + timedelta(minutes=2)).isoformat()
                    storage.save_match(match_id, match)
            else:
                await interaction.followup.send(
                    f"Could not find or parse match data for: {match_id}.",
                    ephemeral=True
                )
                return

        # Determine which team
        if team.value == "a":
            team_name = match["team_a"]
            odds = match["odds_a"]
        else:
            team_name = match["team_b"]
            odds = match["odds_b"]
        
        # Check if user already bet on this map
        existing_bet = storage.get_user_bet_for_map(
            user_id, match_id, match["map_number"]
        )
        if existing_bet:
            await interaction.followup.send(
                f"You already placed a bet on Map {match['map_number']}!",
                ephemeral=True
            )
            return
        
        # Create bet
        from datetime import datetime
        bet_id = storage.generate_bet_id()
        bet_data = {
            "bet_id": bet_id,
            "user_id": user_id,
            "match_id": match_id,
            "map_number": match["map_number"],
            "team": team_name,
            "amount": amount,
            "odds": odds,
            "placed_at": datetime.now().isoformat()
        }
        
        storage.save_bet(bet_id, bet_data)
        
        # Update user balance
        new_balance = user["balance"] - amount
        storage.update_user(user_id, {
            "balance": new_balance,
            "bets_placed": user["bets_placed"] + 1
        })
        
        # Send confirmation
        embed = discord.Embed(
            title="Bet Placed Successfully!",
            description=f"You bet **{amount}** tokens on **{team_name}**",
            color=discord.Color.green()
        )
        embed.add_field(name="Match", value=f"{match['team_a']} vs {match['team_b']}", inline=False)
        embed.add_field(name="Map", value=f"Map {match['map_number']} - {match['current_map']}", inline=False)
        embed.add_field(name="Odds", value=f"{odds}", inline=True)
        embed.add_field(name="Potential Win", value=f"{int(amount * odds)} tokens", inline=True)
        embed.add_field(name="New Balance", value=f"{new_balance} tokens", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @app_commands.command(name="balance", description="Check your balance and stats")
    async def balance_command(self, interaction: discord.Interaction):
        """Display user's balance and statistics."""
        user_id = str(interaction.user.id)
        user = storage.get_or_create_user(user_id)
        
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s Balance",
            color=discord.Color.gold()
        )
        
        embed.add_field(name="Balance", value=f"{user['balance']} tokens", inline=False)
        embed.add_field(name="Total Won", value=f"{user['total_won']} tokens", inline=True)
        embed.add_field(name="Total Lost", value=f"{user['total_lost']} tokens", inline=True)
        embed.add_field(name="Bets Placed", value=f"{user['bets_placed']}", inline=True)
        
        # Calculate profit/loss
        profit = user['total_won'] - user['total_lost']
        embed.add_field(
            name="Profit/Loss",
            value=f"{profit:+d} tokens",
            inline=True
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="leaderboard", description="View top players by balance")
    async def leaderboard_command(self, interaction: discord.Interaction):
        """Display leaderboard of top players."""
        leaderboard = storage.get_leaderboard(10)
        
        embed = discord.Embed(
            title="Leaderboard",
            description="Top players by balance",
            color=discord.Color.purple()
        )
        
        if not leaderboard:
            embed.description = "No players yet!"
        else:
            for i, (user_id, user_data) in enumerate(leaderboard, 1):
                # Try to get user from cache
                user = self.bot.get_user(int(user_id))
                name = user.display_name if user else f"User {user_id[:8]}..."
                
                embed.add_field(
                    name=f"#{i} {name}",
                    value=f"Balance: {user_data['balance']} | Won: {user_data['total_won']}",
                    inline=False
                )
        
        await interaction.response.send_message(embed=embed)
    
    # Admin commands
    @app_commands.command(name="admin_reset_balance", description="[ADMIN] Reset a user's balance")
    @app_commands.describe(user="User to reset", amount="New balance amount (default: 1000)")
    async def admin_reset_balance(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: Optional[int] = 1000
    ):
        """Reset a user's balance (admin only)."""
        if not self.is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to use this command!",
                ephemeral=True
            )
            return
        
        user_id = str(user.id)
        storage.update_user(user_id, {"balance": amount})
        
        await interaction.response.send_message(
            f"Reset {user.display_name}'s balance to {amount} tokens.",
            ephemeral=True
        )
    
    @app_commands.command(name="admin_add_balance", description="[ADMIN] Add balance to a user")
    @app_commands.describe(user="User to add balance to", amount="Amount to add")
    async def admin_add_balance(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int
    ):
        """Add balance to a user (admin only)."""
        if not self.is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to use this command!",
                ephemeral=True
            )
            return
        
        user_id = str(user.id)
        user_data = storage.get_or_create_user(user_id)
        new_balance = user_data["balance"] + amount
        storage.update_user(user_id, {"balance": new_balance})
        
        await interaction.response.send_message(
            f"Added {amount} tokens to {user.display_name}. New balance: {new_balance}",
            ephemeral=True
        )
    
    @app_commands.command(name="admin_cancel_match", description="[ADMIN] Cancel a match and refund all bets")
    @app_commands.describe(match_id="Match ID to cancel")
    async def admin_cancel_match(self, interaction: discord.Interaction, match_id: str):
        """Cancel a match and refund all bets (admin only)."""
        if not self.is_admin(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to use this command!",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Get all bets for this match
        bets = storage.get_match_bets(match_id)
        
        if not bets:
            await interaction.followup.send(
                f"No active bets found for match {match_id}.",
                ephemeral=True
            )
            return
        
        # Refund all bets
        refunded_count = 0
        for bet_id, bet_data in bets.items():
            user_id = bet_data["user_id"]
            amount = bet_data["amount"]
            
            # Get user and refund
            user = storage.get_or_create_user(user_id)
            new_balance = user["balance"] + amount
            storage.update_user(user_id, {"balance": new_balance})
            
            # Remove bet
            storage.remove_bet(bet_id)
            refunded_count += 1
        
        # Remove match
        storage.remove_match(match_id)
        
        await interaction.followup.send(
            f"Cancelled match {match_id} and refunded {refunded_count} bets.",
            ephemeral=True
        )

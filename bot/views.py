"""Discord UI views (buttons, modals) for the betting bot."""

import discord
from discord.ui import Button, Modal, TextInput, View
from datetime import datetime, timedelta

from utils import storage


class BetAmountModal(Modal):
    """Modal for entering bet amount."""
    
    def __init__(self, match_id: str, team: str, odds: float):
        super().__init__(title=f"Place Bet - {team}")
        self.match_id = match_id
        self.team = team
        self.odds = odds
        
        self.amount_input = TextInput(
            label="Bet Amount",
            placeholder="Enter amount (min 10)",
            min_length=1,
            max_length=10,
            required=True
        )
        self.add_item(self.amount_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission."""
        user_id = str(interaction.user.id)
        
        # Validate amount
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message(
                "Please enter a valid number!",
                ephemeral=True
            )
            return
        
        if amount < 10:
            await interaction.response.send_message(
                "Minimum bet is 10 tokens!",
                ephemeral=True
            )
            return
        
        # Get user data
        user = storage.get_or_create_user(user_id)
        
        if user["balance"] < amount:
            await interaction.response.send_message(
                f"Insufficient balance! You have {user['balance']} tokens.",
                ephemeral=True
            )
            return
        
        # Get match data
        match = storage.get_match(self.match_id)
        if not match:
            await interaction.response.send_message(
                "Match no longer available!",
                ephemeral=True
            )
            return

        if "betting_closes_at" in match:
            closes_at = datetime.fromisoformat(match["betting_closes_at"])
            if datetime.now() > closes_at:
                await interaction.response.send_message(
                    "❌ **Betting is closed!** The 2-minute window for this match has expired.",
                    ephemeral=True
                )
                return

        # Check if user already bet on this map
        existing_bet = storage.get_user_bet_for_map(
            user_id, self.match_id, match["map_number"]
        )
        if existing_bet:
            await interaction.response.send_message(
                f"You already placed a bet on Map {match['map_number']}!",
                ephemeral=True
            )
            return
        
        # Create bet
        bet_id = storage.generate_bet_id()
        bet_data = {
            "bet_id": bet_id,
            "user_id": user_id,
            "match_id": self.match_id,
            "map_number": match["map_number"],
            "team": self.team,
            "amount": amount,
            "odds": self.odds,
            "placed_at": datetime.now().isoformat()
        }
        
        storage.save_bet(bet_id, bet_data)
        
        # Deduct balance
        new_balance = user["balance"] - amount
        storage.update_user(user_id, {
            "balance": new_balance,
            "bets_placed": user["bets_placed"] + 1
        })
        
        # Send confirmation
        embed = discord.Embed(
            title="Bet Placed Successfully!",
            description=f"You bet **{amount}** tokens on **{self.team}**",
            color=discord.Color.green()
        )
        embed.add_field(name="Match", value=f"{match['team_a']} vs {match['team_b']}", inline=False)
        embed.add_field(name="Map", value=f"Map {match['map_number']} - {match['current_map']}", inline=False)
        embed.add_field(name="Odds", value=f"{self.odds}", inline=True)
        embed.add_field(name="Potential Win", value=f"{int(amount * self.odds)} tokens", inline=True)
        embed.add_field(name="New Balance", value=f"{new_balance} tokens", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MatchView(View):
    """View with buttons for match betting."""
    
    def __init__(self, match_id: str, team_a: str, team_b: str, odds_a: float, odds_b: float, map_number: int):
        super().__init__(timeout=120)  # 2 minute timeout
        self.match_id = match_id
        self.team_a = team_a
        self.team_b = team_b
        self.odds_a = odds_a
        self.odds_b = odds_b
        self.map_number = map_number
        
        # Create buttons
        self.add_item(BetButton(team_a, odds_a, discord.ButtonStyle.primary, match_id, map_number))
        self.add_item(BetButton(team_b, odds_b, discord.ButtonStyle.secondary, match_id, map_number))
    
    async def on_timeout(self):
        """Disable buttons when view times out."""
        for child in self.children:
            child.disabled = True

        if hasattr(self, 'message'):
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class BetButton(Button):
    """Button for placing a bet on a team."""
    
    def __init__(self, team: str, odds: float, style: discord.ButtonStyle, match_id: str, map_number: int):
        super().__init__(
            label=f"{team} - {odds}",
            style=style,
            custom_id=f"bet_{match_id}_{team}_{map_number}"
        )
        self.team = team
        self.odds = odds
        self.match_id = match_id
    
    async def callback(self, interaction: discord.Interaction):
        """Handle button click."""
        user_id = str(interaction.user.id)
        
        # Check if user already bet on this map
        match = storage.get_match(self.match_id)
        if not match:
            await interaction.response.send_message(
                "This match is no longer available!",
                ephemeral=True
            )
            return

        # Check if betting is closes.
        if "betting_closes_at" in match:
            closes_at = datetime.fromisoformat(match["betting_closes_at"])
            if datetime.now() > closes_at:
                await interaction.response.send_message(
                    "❌ **Betting is closed!** The 2-minute window for this match has expired.",
                    ephemeral=True
                )
                return
        
        existing_bet = storage.get_user_bet_for_map(
            user_id, self.match_id, match["map_number"]
        )
        if existing_bet:
            await interaction.response.send_message(
                f"You already placed a bet on Map {match['map_number']}!",
                ephemeral=True
            )
            return
        
        # Show modal for amount input
        modal = BetAmountModal(self.match_id, self.team, self.odds)
        await interaction.response.send_modal(modal)

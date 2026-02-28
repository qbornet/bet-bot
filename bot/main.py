"""Main entry point for the Discord betting bot."""

import os
import sys
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import discord
from discord.ext import commands, tasks

from utils import storage
from scraper.factory import scrape_any
from scraper.bet_scraper import scraper
from bot.commands import BettingCommands

# Load environment variables
load_dotenv()

# Bot configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required!")

# Bot intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class BettingBot(commands.Bot):
    """Discord bot for betting on CS matches."""
    
    def __init__(self):
        super().__init__(
            command_prefix="!",  # Not used with slash commands
            intents=intents,
            help_command=None
        )
        self.tracked_matches = set()
    
    async def setup_hook(self):
        """Setup bot on startup."""
        print("🤖 Setting up betting bot...")
        
        # Add commands cog
        await self.add_cog(BettingCommands(self))
        
        # Sync commands
        try:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} command(s)")
        except Exception as e:
            print(f"❌ Failed to sync commands: {e}")
        
        # Start background tasks
        self.update_matches.start()
        self.settle_bets.start()
    
    async def on_ready(self):
        """Called when bot is ready."""
        user_display = f"{self.user} (ID: {self.user.id})" if self.user else "Unknown User"
        print(f"✅ Bot is ready! Logged in as {user_display}")
        print(f"📊 Tracking {len(storage.get_all_matches())} matches")
        print(f"💰 Managing {len(storage.get_all_bets())} active bets")
    
    async def on_error(self, event_method, *args, **kwargs):
        """Handle errors."""
        print(f"❌ Error in {event_method}: ", *args)
    
    @tasks.loop(seconds=60)
    async def update_matches(self):
        """Update match data every 60 seconds using thread pool to avoid blocking."""
        import asyncio
        
        matches = storage.get_all_matches()
        
        if not matches:
            return
        
        loop = asyncio.get_event_loop()
        
        for match_id in list(matches.keys()):
            try:
                # Get previous match data for comparison
                prev_match = storage.get_match(match_id)
                
                # Run sync scraper in thread pool to avoid blocking
                updated_match = await asyncio.to_thread(scraper.scrape_match, match_id)
                
                if updated_match:
                    # Store previous scores for settlement tracking
                    if prev_match:
                        updated_match["prev_score_a"] = prev_match.get("score_a", 0)
                        updated_match["prev_score_b"] = prev_match.get("score_b", 0)
                    else:
                        updated_match["prev_score_a"] = 0
                        updated_match["prev_score_b"] = 0
                    
                    storage.save_match(match_id, updated_match)
                    
                    if updated_match["score_a"] >= 2 or updated_match["score_b"] >= 2:
                        print(f"🏁 Match {match_id} has ended - Final: {updated_match['team_a']} {updated_match['score_a']} vs {updated_match['score_b']} {updated_match['team_b']}")
                else:
                    print(f"⚠️ Match {match_id} no longer available")
                    
            except Exception as e:
                print(f"❌ Error updating match {match_id}: {e}")
    
    @update_matches.before_loop
    async def before_update_matches(self):
        """Wait for bot to be ready."""
        await self.wait_until_ready()
    
    @tasks.loop(seconds=60)
    async def settle_bets(self):
        """Check and settle bets every minute."""
        bets = storage.get_all_bets()
        matches = storage.get_all_matches()
        
        if not bets:
            return
        
        for bet_id, bet_data in list(bets.items()):
            try:
                match_id = bet_data["match_id"]
                match = matches.get(match_id)
                
                if not match:
                    print(f"💰 Refunding bet {bet_id} - match not found")
                    await self._refund_bet(bet_data)
                    continue
                
                # Get previous scores from match to determine who won the map
                prev_score_a = match.get("prev_score_a", 0)
                prev_score_b = match.get("prev_score_b", 0)
                current_score_a = match.get("score_a", 0)
                current_score_b = match.get("score_b", 0)
                
                # Check if map has ended by comparing current map number
                if match["map_number"] > bet_data["map_number"]:
                    # Map has ended
                    map_num = bet_data["map_number"]
                    bet_team = bet_data["team"]
                    
                    # Determine who won the map by comparing score changes
                    # If prev_score_a < current_score_a, team A won a map
                    winner = None
                    if current_score_a > prev_score_a:
                        winner = match["team_a"]
                    elif current_score_b > prev_score_b:
                        winner = match["team_b"]
                    
                    if not winner:
                        # Fallback: use current scores
                        if current_score_a > current_score_b:
                            winner = match["team_a"]
                        elif current_score_b > current_score_a:
                            winner = match["team_b"]
                        else:
                            print(f"🗺️ Map {map_num} ended in tie - refunding bet {bet_id}")
                            await self._refund_bet(bet_data)
                            continue
                    
                    if bet_team == winner:
                        print(f"🏆 Bet {bet_id} WON! User {bet_data['user_id']} bet on {bet_team}, winner is {winner}")
                        await self._settle_bet_win(bet_data, match, winner)
                    else:
                        print(f"💔 Bet {bet_id} LOST. User {bet_data['user_id']} bet on {bet_team}, winner is {winner}")
                        await self._settle_bet_loss(bet_data, match, winner)
                    
                    continue
                
                # Check if match has ended (best of 3 - first to 2 maps)
                if current_score_a >= 2 or current_score_b >= 2:
                    # Match ended - determine winner from current scores
                    winner = match["team_a"] if current_score_a >= 2 else match["team_b"]
                    bet_team = bet_data["team"]
                    
                    if bet_team == winner:
                        await self._settle_bet_win(bet_data, match, winner)
                    else:
                        await self._settle_bet_loss(bet_data, match, winner)
                    
                    # Clean up match after settling all bets
                    if not storage.get_match_bets(match_id):
                        storage.remove_match(match_id)
                        
            except Exception as e:
                print(f"❌ Error settling bet {bet_id}: {e}")
    
    async def _refund_bet(self, bet_data: dict):
        """Refund a bet to the user."""
        user_id = bet_data["user_id"]
        amount = bet_data["amount"]
        
        user = storage.get_or_create_user(user_id)
        new_balance = user["balance"] + amount
        storage.update_user(user_id, {"balance": new_balance})
        
        storage.remove_bet(bet_data["bet_id"])
        
        print(f"💸 Refunded {amount} tokens to user {user_id}")
    
    async def _settle_bet_win(self, bet_data: dict, match: dict, winning_team: str):
        """Settle a winning bet and notify user."""
        user_id = bet_data["user_id"]
        amount = bet_data["amount"]
        odds = float(bet_data["odds"])  # Ensure it's a float
        winnings = int(amount * odds)
        profit = winnings - amount
        
        # Get fresh user data
        user = storage.get_or_create_user(user_id)
        old_balance = user["balance"]
        new_balance = old_balance + winnings
        
        print(f"🏆 WIN CALCULATION: bet={amount}, odds={odds}, winnings={winnings}, profit={profit}")
        print(f"🏆 OLD_BALANCE={old_balance} + WINNINGS={winnings} = NEW_BALANCE={new_balance}")
        
        storage.update_user(user_id, {
            "balance": new_balance,
            "total_won": user["total_won"] + profit
        })
        
        storage.remove_bet(bet_data["bet_id"])
        
        print(f"🏆 Bet {bet_data['bet_id']} won! User {user_id} won {profit} tokens (total winnings: {winnings})")
        
        # Notify user via DM
        await self._notify_bet_result(
            user_id, 
            True, 
            amount, 
            profit, 
            winnings, 
            new_balance,
            bet_data["team"],
            winning_team,
            match["team_a"],
            match["team_b"],
            odds
        )
    
    async def _settle_bet_loss(self, bet_data: dict, match: dict, winning_team: str):
        """Settle a losing bet and notify user."""
        user_id = bet_data["user_id"]
        amount = bet_data["amount"]
        
        user = storage.get_or_create_user(user_id)
        storage.update_user(user_id, {
            "total_lost": user["total_lost"] + amount
        })
        
        storage.remove_bet(bet_data["bet_id"])
        
        print(f"💔 Bet {bet_data['bet_id']} lost. User {user_id} lost {amount} tokens")
        
        # Notify user via DM
        await self._notify_bet_result(
            user_id, 
            False, 
            amount, 
            -amount, 
            0, 
            user["balance"],
            bet_data["team"],
            winning_team,
            match["team_a"],
            match["team_b"],
            bet_data.get("odds", 1.0)
        )
    
    async def _notify_bet_result(self, user_id: str, won: bool, bet_amount: int, profit: int, winnings: int, new_balance: int, bet_team: str, winning_team: str, team_a: str, team_b: str, odds: float = 1.0):
        """Send DM to user about bet result."""
        import discord
        
        user = self.get_user(int(user_id))
        if not user:
            return
        
        embed = discord.Embed(
            title=f"🎰 Bet Result - {'WIN' if won else 'LOSS'}",
            color=discord.Color.green() if won else discord.Color.red()
        )
        
        embed.add_field(name="Match", value=f"{team_a} vs {team_b}", inline=False)
        embed.add_field(name="You bet on", value=bet_team, inline=True)
        embed.add_field(name="Winner", value=winning_team, inline=True)
        embed.add_field(name="Bet Amount", value=f"{bet_amount} tokens", inline=True)
        
        if won:
            embed.add_field(name="Odds", value=str(odds), inline=True)
            embed.add_field(name="Profit", value=f"+{profit} tokens", inline=True)
            embed.add_field(name="Winnings", value=f"{winnings} tokens", inline=True)
        else:
            embed.add_field(name="Loss", value=f"-{bet_amount} tokens", inline=True)
        
        embed.add_field(name="New Balance", value=f"💰 {new_balance} tokens", inline=False)
        
        try:
            await user.send(embed=embed)
        except Exception as e:
            print(f"❌ Could not DM user {user_id}: {e}")
    
    @settle_bets.before_loop
    async def before_settle_bets(self):
        """Wait for bot to be ready."""
        await self.wait_until_ready()


def main():
    """Main entry point."""
    print("🎮 Starting Discord Betting Bot...")
    print("=" * 50)
    
    bot = BettingBot()
    
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
    finally:
        scraper.close()
        print("🔒 Cleaned up resources")


if __name__ == "__main__":
    main()

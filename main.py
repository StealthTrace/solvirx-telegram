from typing import Final, Dict, List, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import json
import aiohttp
import asyncio
import os
from datetime import datetime
import re
from dotenv import load_dotenv
import random  # For jitter in retry backoff

# Load environment variables
load_dotenv('../.env.local')  

# Get tokens from environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8276149479:AAEbK3YWKU37QxuLbclk0U-T3Lhepy-cojw')
SOLANA_TRACKER_API_KEY = os.getenv('SOLANA_TRACKER_API_KEY', '151b4151-3227-4eb2-96d8-0b19be6fa5a6')


# Use environment variable for bot token if available
TOKEN: Final = TELEGRAM_TOKEN
BOT_USERNAME: Final = '@Solvirx_bot'

# Mock tokens to use as fallback when API fails
MOCK_TOKENS = [
    {
        "address": "mock1111111111111111111111111111111111111",
        "name": "Mock Token 1",
        "symbol": "MOCK1",
        "deployer": "mockdeployer111111111111111111111111111111",
        "creator": "mockcreator1111111111111111111111111111111",
        "website": "https://mocktoken1.com",
        "twitter": "@mocktoken1"
    },
    {
        "address": "mock2222222222222222222222222222222222222",
        "name": "Mock Token 2",
        "symbol": "MOCK2",
        "deployer": "mockdeployer222222222222222222222222222222",
        "creator": "mockcreator2222222222222222222222222222222",
        "website": "https://mocktoken2.com",
        "twitter": "@mocktoken2"
    }
]

# Define types similar to the React component
class WatchlistFilter:
    def __init__(self, filter_id: str, filter_type: str, value: str):
        self.id = filter_id
        self.type = filter_type  # "twitter" | "website" | "token" | "wallet"
        self.value = value

class MatchHistoryEntry:
    def __init__(self, token: Dict, timestamp: int, filter_obj: WatchlistFilter):
        self.token = token
        self.timestamp = timestamp
        self.filter = filter_obj

# Global data storage
user_filters: Dict[int, List[WatchlistFilter]] = {}
user_matched_tokens: Dict[int, List[Dict]] = {}
user_match_history: Dict[int, List[MatchHistoryEntry]] = {}
user_sound_enabled: Dict[int, bool] = {}
polling_tasks: Dict[str, asyncio.Task] = {} 
polling_intervals: Dict[int, int] = {}  # user_id -> seconds (for configurable intervals)

# Add these at the module level, outside of any functions
token_cache = {}  # In-memory cache
last_fetch_time = 0
cached_response = None

# Add a global cache for Believe tokens
believe_token_cache = {}
last_believe_fetch_time = 0

# Helper functions similar to the React component
def normalize_twitter_handle(handle: str) -> str:
    if not handle:
        return ""
    
    normalized = handle.lower()
    
    # Extract handle from URL if it's a full Twitter URL
    twitter_url_regex = r'(?:https?:\/\/)?(?:www\.)?(?:twitter\.com|x\.com)\/(?:#!\/)?@?([^/?\s]+)'
    match = re.search(twitter_url_regex, normalized)
    if match and match.group(1):
        normalized = match.group(1)
    
    # Remove @ symbol if present
    if normalized.startswith('@'):
        normalized = normalized[1:]
    
    return normalized

def normalize_url(url: str) -> str:
    if not url:
        return ""
    
    try:
        # Remove protocol, trailing slashes, and convert to lowercase
        return re.sub(r'^(https?:\/\/)?(www\.)?', '', url.lower()).rstrip('/')
    except Exception:
        return url.lower()

def are_filters_equal(filter1: WatchlistFilter, filter2: WatchlistFilter) -> bool:
    if filter1.type != filter2.type:
        return False
    
    # Normalize values based on filter type before comparing
    if filter1.type == "twitter":
        return normalize_twitter_handle(filter1.value) == normalize_twitter_handle(filter2.value)
    elif filter1.type == "website":
        return normalize_url(filter1.value) == normalize_url(filter2.value)
    elif filter1.type in ["token", "wallet"]:
        return filter1.value.lower() == filter2.value.lower()
    elif filter1.type == "believe":
        # Compare numeric values for believe filters
        try:
            return int(filter1.value) == int(filter2.value)
        except ValueError:
            return filter1.value == filter2.value
    else:
        return filter1.value == filter2.value

def check_token_match(token: Dict, filters: List[WatchlistFilter]) -> WatchlistFilter:
    if not token or not filters:
        return None
    
    for filter_obj in filters:
        value = filter_obj.value.lower()
        is_match = False
        match_reason = ""
        
        # Handle the new believe filter type
        if filter_obj.type == "believe":
            # Check if this token is from Believe (we tagged it in fetch_believe_tokens)
            if token.get('source') == "believe":
                # Always match - the filtering by followers was done at fetch time
                is_match = True
                min_followers = int(value) if value.isdigit() else 0
                followers = token.get('twitterFollowers', 0)
                match_reason = f"Believe token with {followers} followers (min: {min_followers})"
        
        elif filter_obj.type == "twitter":
            # Normalize the filter value
            normalized_filter_value = normalize_twitter_handle(value)
            
            # Normalize the token's Twitter data
            normalized_twitter = normalize_twitter_handle(token.get('twitter', ''))
            normalized_twitter_username = normalize_twitter_handle(token.get('twitterUsername', ''))
            
            # Check for matches in any of the normalized fields
            if normalized_twitter and normalized_filter_value in normalized_twitter:
                is_match = True
                match_reason = f"Twitter handle match: {normalized_twitter} contains {normalized_filter_value}"
            elif normalized_twitter_username and normalized_filter_value in normalized_twitter_username:
                is_match = True
                match_reason = f"Twitter username match: {normalized_twitter_username} contains {normalized_filter_value}"
        
        elif filter_obj.type == "website":
            token_website = token.get('website', '').lower()
            if token_website and value in token_website:
                is_match = True
                match_reason = f"Website match: {token_website} contains {value}"
        
        elif filter_obj.type == "token":
            token_name = token.get('name', '').lower()
            token_symbol = token.get('symbol', '').lower()
            token_address = token.get('address', '').lower()
            
            # Check exact match for address if the value looks like an address
            if len(value) > 30:  # Solana addresses are long - this is likely an address
                if token_address == value:
                    is_match = True
                    match_reason = f"Address exact match: {token_address}"
            else:
                # Otherwise check for name/symbol contains
                if value in token_name:
                    is_match = True
                    match_reason = f"Name match: {token_name} contains {value}"
                elif value in token_symbol:
                    is_match = True
                    match_reason = f"Symbol match: {token_symbol} contains {value}"
        
        elif filter_obj.type == "wallet":
            token_deployer = token.get('deployer', '').lower()
            token_creator = token.get('creator', '').lower()
            
            if token_deployer == value:
                is_match = True
                match_reason = f"Deployer match: {token_deployer}"
            elif token_creator == value:
                is_match = True
                match_reason = f"Creator match: {token_creator}"
        
        if is_match:
            print(f"MATCH: {token.get('symbol', 'Unknown')} - {match_reason}")  # Keep this as important notification
            return filter_obj
    
    return None

# Save and load user data
def save_user_data(user_id: int):
    # Create directory if it doesn't exist
    os.makedirs('user_data', exist_ok=True)
    
    # Save filters
    if user_id in user_filters:
        filters_data = [{'id': f.id, 'type': f.type, 'value': f.value} for f in user_filters[user_id]]
        with open(f'user_data/{user_id}_filters.json', 'w') as f:
            json.dump(filters_data, f)
    
    # Save matched tokens
    if user_id in user_matched_tokens:
        with open(f'user_data/{user_id}_matched_tokens.json', 'w') as f:
            json.dump(user_matched_tokens[user_id], f)
    
    # Save match history
    if user_id in user_match_history:
        history_data = [{'token': entry.token, 'timestamp': entry.timestamp, 
                         'filter': {'id': entry.filter.id, 'type': entry.filter.type, 'value': entry.filter.value}} 
                        for entry in user_match_history[user_id]]
        with open(f'user_data/{user_id}_history.json', 'w') as f:
            json.dump(history_data, f)
    
    # Save sound preferences
    if user_id in user_sound_enabled:
        with open(f'user_data/{user_id}_sound.json', 'w') as f:
            json.dump({'enabled': user_sound_enabled[user_id]}, f)

def load_user_data(user_id: int):
    try:
        # Load filters
        try:
            with open(f'user_data/{user_id}_filters.json', 'r') as f:
                filters_data = json.load(f)
                user_filters[user_id] = [WatchlistFilter(f['id'], f['type'], f['value']) for f in filters_data]
        except FileNotFoundError:
            user_filters[user_id] = []
        
        # Load matched tokens
        try:
            with open(f'user_data/{user_id}_matched_tokens.json', 'r') as f:
                user_matched_tokens[user_id] = json.load(f)
        except FileNotFoundError:
            user_matched_tokens[user_id] = []
        
        # Load match history
        try:
            with open(f'user_data/{user_id}_history.json', 'r') as f:
                history_data = json.load(f)
                user_match_history[user_id] = []
                for entry in history_data:
                    filter_data = entry['filter']
                    filter_obj = WatchlistFilter(filter_data['id'], filter_data['type'], filter_data['value'])
                    user_match_history[user_id].append(MatchHistoryEntry(entry['token'], entry['timestamp'], filter_obj))
        except FileNotFoundError:
            user_match_history[user_id] = []
        
        # Load sound preferences
        try:
            with open(f'user_data/{user_id}_sound.json', 'r') as f:
                sound_data = json.load(f)
                user_sound_enabled[user_id] = sound_data['enabled']
        except FileNotFoundError:
            user_sound_enabled[user_id] = True
    
    except Exception as e:
        print(f"Error loading user data for {user_id}: {e}")

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the main menu with clickable buttons"""
    keyboard = [
        [InlineKeyboardButton("➕ Add Filter", callback_data="menu_add_filter")],
        [InlineKeyboardButton("📋 List Filters", callback_data="list_filters")],
        [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
        [InlineKeyboardButton("⏹️ Stop Tracking", callback_data="stop_tracking")],
        [InlineKeyboardButton("⚡ Believe Tracker", callback_data="menu_believe")],
        [InlineKeyboardButton("🪙 Matched Tokens", callback_data="matches")],
        [InlineKeyboardButton("📜 Match History", callback_data="history")],
        [InlineKeyboardButton("🧹 Clear Matches", callback_data="clear_matches")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 *Solvirx Token Tracker*\n\n"
        "CA: 61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi\n\n"
        "Our Twitter: [Solvirx](https://x.com/solvirx)\n\n"
        "Choose an option from the menu below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# Commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the command /start is issued."""
    user_id = update.effective_user.id
    
    # Initialize user data if not already
    if user_id not in user_filters:
        user_filters[user_id] = []
        user_matched_tokens[user_id] = []
        user_match_history[user_id] = []
        user_sound_enabled[user_id] = True
        
        # Try to load existing user data
        load_user_data(user_id)
    
    welcome_message = (
        "👋 *Welcome to the Solvirx Token Tracker Bot!*\n\n"
        "CA: 61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi\n\n"
        "Get our latest news on Twitter: [Solvirx](https://x.com/solvirx)\n\n"
        "I'll help you track tokens based on your filters."
    )
    
    # Add extra instructions for group chats
    if update.effective_chat.type in ['group', 'supergroup']:
        welcome_message += ("\n\n*Group Chat Instructions:*\n"
                          f"In this group, you must mention me with {BOT_USERNAME} after commands.\n"
                          f"Example: /help{BOT_USERNAME} ")
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=welcome_message,
        parse_mode='Markdown'
    )
    
    # Show the main menu
    await main_menu(update, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks from inline keyboards"""
    query = update.callback_query
    await query.answer()  # Answer the callback query to stop loading animation
    
    data = query.data
    user_id = update.effective_user.id
    
    # Handle different button actions
    if data == "menu_add_filter":
        # Show filter type selection menu
        keyboard = [
            [InlineKeyboardButton("🪙 Token", callback_data="add_filter_token")],
            [InlineKeyboardButton("⚡ Believe", callback_data="add_filter_believe")],
            [InlineKeyboardButton("🐦 Twitter", callback_data="add_filter_twitter")],
            [InlineKeyboardButton("🌐 Website", callback_data="add_filter_website")],
            [InlineKeyboardButton("👛 Wallet", callback_data="add_filter_wallet")],
            [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="Select filter type to add:",
            reply_markup=reply_markup
        )
    
    elif data == "main_menu":
        # Return to main menu
        keyboard = [
            [InlineKeyboardButton("➕ Add Filter", callback_data="menu_add_filter")],
            [InlineKeyboardButton("📋 List Filters", callback_data="list_filters")],
            [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
            [InlineKeyboardButton("⏹️ Stop Tracking", callback_data="stop_tracking")],
            [InlineKeyboardButton("⚡ Believe Tracker", callback_data="menu_believe")],
            [InlineKeyboardButton("🪙 Matched Tokens", callback_data="matches")],
            [InlineKeyboardButton("📜 Match History", callback_data="history")],
            [InlineKeyboardButton("🧹 Clear Matches", callback_data="clear_matches")]  
      ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="🤖 *Solvirx Token Tracker*\n\n"
                 "CA: 61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi\n\n"
                 "Our Twitter: [Solvirx](https://x.com/solvirx)\n\n"
                 "Choose an option from the menu below:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif data.startswith("add_filter_"):
        # Store the filter type in user context for the next step
        filter_type = data.replace("add_filter_", "")
        context.user_data["filter_type"] = filter_type
        
        # Prompt user to enter filter value
        instructions = {
            "token": "Enter the token name, symbol, or address:",
            "twitter": "Enter the Twitter handle or URL:",
            "website": "Enter the website URL or domain:",
            "wallet": "Enter the wallet address:",
            "believe": "Enter minimum follower count (number only):"
        }
        
        await query.edit_message_text(
            text=f"📝 {instructions.get(filter_type, 'Enter filter value:')}\n\n"
                 f"Please type your response below. (if in groups reply to this message)\n"
        )
        
        # Set user state to waiting for filter value
        context.user_data["awaiting_input"] = "filter_value"
    
    elif data.startswith("quick_add_token_"):
        # Extract token symbol from callback data
        token_symbol = data.replace("quick_add_token_", "")
        
        # Create the filter
        filter_id = str(int(datetime.now().timestamp()))
        new_filter = WatchlistFilter(filter_id, "token", token_symbol)
        
        # Check for duplicates
        if user_id not in user_filters:
            user_filters[user_id] = []
        
        duplicate = False
        for existing_filter in user_filters[user_id]:
            if are_filters_equal(existing_filter, new_filter):
                duplicate = True
                break
        
        if not duplicate:
            user_filters[user_id].append(new_filter)
            save_user_data(user_id)
            
            await query.edit_message_text(
                text=f"✅ Added filter for token: {token_symbol}\n\nNow you can start tracking or go back to the menu.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )
        else:
            await query.edit_message_text(
                text=f"⚠️ Filter for token {token_symbol} already exists.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )

    elif data.startswith("quick_add_twitter_"):
        # Extract Twitter handle from callback data
        twitter_handle = data.replace("quick_add_twitter_", "")
        
        # Create the filter
        filter_id = str(int(datetime.now().timestamp()))
        new_filter = WatchlistFilter(filter_id, "twitter", twitter_handle)
        
        # Check for duplicates
        if user_id not in user_filters:
            user_filters[user_id] = []
        
        duplicate = False
        for existing_filter in user_filters[user_id]:
            if are_filters_equal(existing_filter, new_filter):
                duplicate = True
                break
        
        if not duplicate:
            user_filters[user_id].append(new_filter)
            save_user_data(user_id)
            
            await query.edit_message_text(
                text=f"✅ Added filter for Twitter: {twitter_handle}\n\nNow you can start tracking or go back to the menu.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )
        else:
            await query.edit_message_text(
                text=f"⚠️ Filter for Twitter handle {twitter_handle} already exists.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )

    elif data.startswith("quick_add_believe_"):
        # Extract follower count from callback data
        followers = data.replace("quick_add_believe_", "")
        
        # Create the filter
        filter_id = str(int(datetime.now().timestamp()))
        new_filter = WatchlistFilter(filter_id, "believe", followers)
        
        # Check for duplicates
        if user_id not in user_filters:
            user_filters[user_id] = []
        
        duplicate = False
        for existing_filter in user_filters[user_id]:
            if are_filters_equal(existing_filter, new_filter):
                duplicate = True
                break
        
        if not duplicate:
            user_filters[user_id].append(new_filter)
            save_user_data(user_id)
            
            await query.edit_message_text(
                text=f"✅ Added filter for Believe tokens with {followers}+ followers\n\nNow you can start tracking or go back to the menu.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )
        else:
            await query.edit_message_text(
                text=f"⚠️ Filter for Believe tokens with {followers}+ followers already exists.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )
        
    elif data.startswith("remove_filter_"):
        # Extract filter index from callback data
        try:
            index = int(data.replace("remove_filter_", ""))
            
            if user_id not in user_filters or index < 0 or index >= len(user_filters[user_id]):
                await query.edit_message_text(
                    text="❌ Invalid filter index.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("« Back to Filters", callback_data="list_filters")]
                    ])
                )
                return
            
            removed = user_filters[user_id].pop(index)
            save_user_data(user_id)
            
            await query.edit_message_text(
                text=f"✅ Removed filter: {removed.type} - {removed.value}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Filters", callback_data="list_filters")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
                ])
            )
        except ValueError:
            await query.edit_message_text(
                text="❌ Invalid filter index format.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Filters", callback_data="list_filters")]
                ])
            )
    
    # Handle other buttons by redirecting to existing command handlers
    elif data == "list_filters":
        await list_filters_callback(update, context)
    elif data == "start_tracking":
        await start_tracking_callback(update, context)
    elif data == "stop_tracking":
        await stop_tracking_callback(update, context)
    elif data == "matches":
        await matches_callback(update, context)
    elif data == "history":
        await history_callback(update, context)
    elif data == "clear_matches":
        await clear_matches_callback(update, context)
    elif data == "refresh":
        await refresh_callback(update, context)
    elif data == "menu_believe":
        # Show believe tracker options
        keyboard = [
            [InlineKeyboardButton("0+ Followers", callback_data="believe_0")],
            [InlineKeyboardButton("100+ Followers", callback_data="believe_100")],
            [InlineKeyboardButton("500+ Followers", callback_data="believe_500")],
            [InlineKeyboardButton("1000+ Followers", callback_data="believe_1000")],
            [InlineKeyboardButton("Custom", callback_data="believe_custom")],
            [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="Select minimum Twitter followers for Believe tokens:",
            reply_markup=reply_markup
        )
        
    elif data.startswith("believe_"):
        if data == "believe_custom":
            context.user_data["awaiting_input"] = "believe_followers"
            await query.edit_message_text(
                text="Enter the minimum number of followers:"
            )
        else:
            # Extract follower count from callback data
            followers = data.replace("believe_", "")
            await believe_tracker_with_followers(update, context, followers)

def check_group_mention(func):
    """Decorator to ensure bot only responds to mentioned commands in groups"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Private chats always respond
        if update.effective_chat.type == 'private':
            return await func(update, context)
        
        # In groups, check if command has mention
        if update.effective_chat.type in ['group', 'supergroup']:
            message = update.message
            # Check if message text exists and contains the bot username
            if not message or not message.text:
                return
            
            # Commands start with /, check if there's a mention
            entities = message.entities or []
            has_bot_mention = False
            
            # Either the command itself has @botusername format
            # or there's a separate mention entity with the bot username
            for entity in entities:
                if entity.type == 'mention' and BOT_USERNAME.lower() in message.text[entity.offset:entity.offset+entity.length].lower():
                    has_bot_mention = True
                    break
                elif entity.type == 'bot_command':
                    command_text = message.text[entity.offset:entity.offset+entity.length]
                    if '@' in command_text and BOT_USERNAME.lower().replace('@', '') in command_text.lower():
                        has_bot_mention = True
                        break
            
            if not has_bot_mention:
                # Silently ignore commands without mention in groups
                return
                
        # Continue with the original function
        return await func(update, context)
    
    return wrapper

async def list_filters_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show filters with inline keyboard"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id not in user_filters or not user_filters[user_id]:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="You don't have any filters yet. Add one from the main menu.",
            reply_markup=reply_markup
        )
        return
    
    filters_text = "📋 Your current filters:\n\n"
    
    # Create buttons for each filter with option to remove
    keyboard = []
    for i, filter_obj in enumerate(user_filters[user_id], 1):
        filters_text += f"{i}. Type: {filter_obj.type}, Value: {filter_obj.value}\n"
        keyboard.append([InlineKeyboardButton(
            f"❌ Remove #{i}: {filter_obj.type} - {filter_obj.value[:20]}", 
            callback_data=f"remove_filter_{i-1}"
        )])
    
    keyboard.append([InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=filters_text,
        reply_markup=reply_markup
    )

async def start_tracking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start tracking from button press"""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Check if user has filters
    if user_id not in user_filters or not user_filters[user_id]:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="❌ You need to add filters first.",
            reply_markup=reply_markup
        )
        return
    
    # Create a unique key for this user-chat combination
    tracking_key = get_tracking_key(user_id, chat_id)
    
    # Check if tracking is already running in this chat
    if tracking_key in polling_tasks and not polling_tasks[tracking_key].done():
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="⚠️ Token tracking is already running in this chat.",
            reply_markup=reply_markup
        )
        return
    
    # Start tracking task specific to this chat
    task = asyncio.create_task(tracking_task_callback(update, context, user_id, chat_id))
    polling_tasks[tracking_key] = task
    
    keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="🔍 Token tracking started. You'll be notified of any matches.",
        reply_markup=reply_markup
    )

# Update tracking_task_callback to use the specific chat_id
async def tracking_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    """Modified tracking task that works with callbacks"""
    try:
        # Get user's preferred polling interval or use default 5 seconds
        polling_interval = polling_intervals.get(user_id, 5)
        
        # Create a tracking key for this specific chat
        tracking_key = get_tracking_key(user_id, chat_id)
        
        # Initialize a set to keep track of previously seen tokens for this specific tracking task
        if not hasattr(context, 'chat_matched_tokens'):
            context.chat_matched_tokens = {}
        if tracking_key not in context.chat_matched_tokens:
            context.chat_matched_tokens[tracking_key] = set()
        
        # Categorize filters by type
        believe_filters = []
        other_filters = []
        
        if user_id in user_filters:
            for filter_obj in user_filters[user_id]:
                if filter_obj.type == "believe":
                    believe_filters.append(filter_obj)
                else:
                    other_filters.append(filter_obj)
        
        while True:
            all_tokens = []
            
            # Only fetch from Solana API if there are non-believe filters
            if other_filters:
                solana_tokens = await fetch_latest_tokens(force_refresh=True)
                if solana_tokens:
                    all_tokens.extend(solana_tokens)
            
            # Fetch from Believe API if there are believe filters
            if believe_filters:
                for filter_obj in believe_filters:
                    try:
                        min_followers = int(filter_obj.value)
                        believe_tokens = await fetch_believe_tokens(min_followers=min_followers, force_refresh=True)
                        if believe_tokens:
                            all_tokens.extend(believe_tokens)
                    except Exception as e:
                        print(f"Error fetching Believe tokens for filter {filter_obj.value}: {str(e)}")
            
            # Process tokens against user filters
            matches = []
            
            # Find matches that haven't been seen in THIS chat yet
            for token in all_tokens:
                if 'address' in token and token['address'] in context.chat_matched_tokens[tracking_key]:
                    # Skip tokens we've already matched in this chat
                    continue
                
                # Check if any filter matches
                matched_filter = check_token_match(token, user_filters.get(user_id, []))
                if matched_filter:
                    # Add to global user matches - this is for the /matches command
                    if user_id not in user_matched_tokens:
                        user_matched_tokens[user_id] = []
                    
                    # Check if it's already in the global matches
                    is_new_globally = True
                    for existing_token in user_matched_tokens[user_id]:
                        if 'address' in existing_token and 'address' in token and existing_token['address'] == token['address']:
                            is_new_globally = False
                            break
                    
                    # Add to global matches if it's new
                    if is_new_globally:
                        user_matched_tokens[user_id].append(token)
                        
                        # Add to match history
                        if user_id not in user_match_history:
                            user_match_history[user_id] = []
                        
                        user_match_history[user_id].append(
                            MatchHistoryEntry(token, int(datetime.now().timestamp() * 1000), matched_filter)
                        )
                        
                        # Save user data
                        save_user_data(user_id)
                    
                    # Mark this token as seen for this specific chat
                    if 'address' in token:
                        context.chat_matched_tokens[tracking_key].add(token['address'])
                    
                    # Add to current batch of matches for notification
                    matches.append({"token": token, "filter": matched_filter})
            
            # Notify user if matches found - send to the specific chat
            if matches:
                notification = f"🚨 Found {len(matches)} new matching tokens! 🚨\n\n"
                
                for i, match in enumerate(matches[:5], 1):  # Limit to 5 in notification
                    token = match["token"]
                    filter_obj = match["filter"]
                    
                    # Determine the source
                    source = "Believe" if token.get('source') == 'believe' else "Solana"
                    
                    symbol = token.get('symbol', 'Unknown')
                    name = token.get('name', 'Unknown')
                    address = token.get('address', 'Unknown')
                    website = token.get('website', '')
                    twitter = token.get('twitter', '')
                    
                    notification += f"{i}. {symbol} ({name}) - {source}\n"
                    notification += f"   🔗 CA: `{address}`\n"
                    
                    if website:
                        notification += f"   🌐 Website: {website}\n"
                    
                    if twitter:
                        if not twitter.startswith('@') and not twitter.startswith('http'):
                            twitter = f"@{twitter}"
                        notification += f"   🐦 Twitter: {twitter}\n"
                    
                    notification += f"   Matched filter: {filter_obj.type} - {filter_obj.value}\n\n"
                
                if len(matches) > 5:
                    notification += f"...and {len(matches) - 5} more. Check matches for all."
                
                keyboard = [[InlineKeyboardButton("View All Matches", callback_data="matches")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Send notification to the specific chat
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=notification,
                    reply_markup=reply_markup
                )
            
            # Wait for next check
            await asyncio.sleep(polling_interval)
    except asyncio.CancelledError:
        # This is expected when stopping the task
        pass
    except Exception as e:
        print(f"Error in tracking task: {e}")
        # Send error notification
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Token tracking error: {str(e)}"
        )

async def handle_filter_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for filter values"""
    user_id = update.effective_user.id
    message_type = update.message.chat.type
    text = update.message.text.strip()
    
    # Check if we're in a group chat
    is_group = message_type in ['group', 'supergroup']
    
    # If we're in a group, check if the message is targeting our bot
    if is_group:
        # For awaiting input in groups, we need to either:
        # 1. Check if the message is a direct reply to our bot's message
        # 2. Or check if the bot is mentioned in the text
        is_reply_to_bot = False
        if update.message.reply_to_message and hasattr(update.message.reply_to_message, 'from_user'):
            # Check if the message is replying to our bot
            replied_to_user = update.message.reply_to_message.from_user
            is_reply_to_bot = (replied_to_user.username == BOT_USERNAME.replace('@', ''))
        
        # Check if bot is mentioned in the message
        is_mentioned = BOT_USERNAME.lower() in text.lower()
        
        # Print debug info
        print(f"Message in group: '{text}'")
        print(f"Is reply to bot: {is_reply_to_bot}")
        print(f"Bot mentioned: {is_mentioned}")
        
        # If it's not a reply to our bot and our bot is not mentioned, ignore in groups
        if not is_reply_to_bot and not is_mentioned:
            print("Ignoring message in group (not targeting our bot)")
            return
        
        # If it's a mention, extract just the message part
        if is_mentioned:
            # Extract the actual text without the mention
            parts = text.lower().split(BOT_USERNAME.lower(), 1)
            if len(parts) > 1:
                text = parts[1].strip()
            else:
                text = ""
    
    # Now see if we're awaiting input
    if not context or not hasattr(context, 'user_data') or not context.user_data or not context.user_data.get("awaiting_input"):
        # If we're not in a filter input state and we reached this point (direct message or mentioned in group)
        await handle_message(update, context)
        return
    
    # Process filter input normally as this is an expected response
    input_type = context.user_data.get("awaiting_input")
    user_input = text
    
    # Now process the user input based on what we're expecting
    if input_type == "filter_value":
        filter_type = context.user_data.get("filter_type")
        
        # Validate input based on filter type
        if filter_type == "believe" and not user_input.isdigit():
            await update.message.reply_text(
                "❌ Please enter a valid number for minimum followers."
            )
            return
        
        # Process the filter addition
        new_filter = WatchlistFilter(
            filter_id=str(int(datetime.now().timestamp())),
            filter_type=filter_type,
            value=user_input
        )
        
        # Check for duplicates
        if user_id not in user_filters:
            user_filters[user_id] = []
        
        for existing_filter in user_filters[user_id]:
            if are_filters_equal(existing_filter, new_filter):
                # Show main menu with error message
                keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    "❌ This filter already exists!",
                    reply_markup=reply_markup
                )
                
                # Clear awaiting input state
                context.user_data.pop("awaiting_input", None)
                context.user_data.pop("filter_type", None)
                return
        
        # Add filter
        user_filters[user_id].append(new_filter)
        save_user_data(user_id)
        
        # Show success message with options
        keyboard = [
            [InlineKeyboardButton("➕ Add Another Filter", callback_data="menu_add_filter")],
            [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
            [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ Added new filter: {filter_type} - {user_input}",
            reply_markup=reply_markup
        )
        
        # Clear awaiting input state
        context.user_data.pop("awaiting_input", None)
        context.user_data.pop("filter_type", None)
    
    elif input_type == "believe_followers":
        # Process custom follower count for Believe tracker
        if not user_input.isdigit():
            await update.message.reply_text(
                "❌ Please enter a valid number for minimum followers."
            )
            return
        
        # Run the believe tracker with custom followers
        await believe_tracker_with_followers(update, context, user_input)
        
        # Clear awaiting input state
        context.user_data.pop("awaiting_input", None)


async def believe_tracker_with_followers(update: Update, context: ContextTypes.DEFAULT_TYPE, followers_str: str):
    """Run the Believe tracker with specified followers count"""
    try:
        min_followers = int(followers_str)
        
        # If this is a callback query, use reply_text on the message
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=f"🔍 Fetching tokens from Believe with minimum {min_followers} followers..."
            )
            # Use the original chat_id for the new message
            chat_id = update.effective_chat.id
        else:
            # This is a direct message
            await update.message.reply_text(
                f"🔍 Fetching tokens from Believe with minimum {min_followers} followers..."
            )
            chat_id = update.effective_chat.id
        
        # Fetch the tokens with your existing function
        tokens = await fetch_believe_tokens(min_followers=min_followers, force_refresh=True)
        
        if not tokens:
            keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ No tokens found or API error occurred.",
                reply_markup=reply_markup
            )
            return
        
        # Format results message
        tokens_text = f"✅ Found {len(tokens)} tokens with {min_followers}+ followers on Believe.\n\n"
        
        # Create buttons for each token to easily add as filter
        keyboard = []
        
        # Add up to 5 tokens to the message
        for i, token in enumerate(tokens[:5], 1):
            coin_name = token.get('name', 'Unknown')
            coin_ticker = token.get('symbol', 'Unknown')
            twitter_handle = normalize_twitter_handle(token.get('twitter', ''))
            followers_count = token.get('twitterFollowers', 0)
            
            tokens_text += f"{i}. {coin_name} (${coin_ticker})\n"
            if twitter_handle:
                tokens_text += f"   🐦 Twitter: @{twitter_handle} ({followers_count:,} followers)\n"
            
            # Add buttons to track this token
            if coin_ticker:
                keyboard.append([InlineKeyboardButton(
                    f"➕ Track ${coin_ticker}",
                    callback_data=f"quick_add_token_{coin_ticker}"
                )])
            
            if twitter_handle:
                keyboard.append([InlineKeyboardButton(
                    f"➕ Track @{twitter_handle}",
                    callback_data=f"quick_add_twitter_{twitter_handle}"
                )])
        
        # Add button to track all Believe tokens with this follower count
        keyboard.append([InlineKeyboardButton(
            f"➕ Track All Believe ({min_followers}+ followers)",
            callback_data=f"quick_add_believe_{min_followers}"
        )])
        
        keyboard.append([InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send as a new message
        await context.bot.send_message(
            chat_id=chat_id,
            text=tokens_text,
            reply_markup=reply_markup
        )
        
    except Exception as e:
        # Handle errors
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error: {str(e)}",
            reply_markup=reply_markup
        )


# Update the help_command to include the new believe filter
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message when the command /help is issued."""
    help_message = (
        "CA: 61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi\n\n"
        "Our twitter: https://x.com/solvirx \n\n"
        " Solvirx Token Tracker Commands: \n\n"
        "/menu - Show clickable menu with all options\n\n"
        "/add_filter <type> <value> - Add a filter\n"
        "   Types: believe, token, twitter, website, wallet \n"
        "   Examples:\n"
        "   /add_filter believe 100 (tracks Believe tokens with 100+ followers)\n"
        "   /add_filter token pump (tracks any token that has name pump)\n\n"
        "!! WHEN USING A FILTER DONT FORGET TO START TRACKING USING /start_tracking !!\n\n"
        "/believe_tracker <min_followers> - One-time fetch from Believe\n\n"
        "   Example: /believe_tracker 100\n"
        "/list_filters - Show your current filters and the index of each filter\n\n"
        "/remove_filter <index> - Remove a filter by index\n"
        "   Example: /remove_filter 1\n\n"
        "/matches - Show your matched tokens\n\n"
        "/history - Show your match history\n\n"
        "/start_tracking - Start automatic token tracking\n\n"
        "/stop_tracking - Stop automatic tracking\n\n"
        "/clear_matches - Clear all matched tokens\n\n"
        )
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_message)


# Add this function before the tracking_task function
async def fetch_believe_tokens(min_followers=0, force_refresh=False):
    global last_believe_fetch_time, believe_token_cache
    
    # Rate limiting check (4 seconds minimum between requests)
    now = int(datetime.now().timestamp() * 1000)
    time_since_last_fetch = now - last_believe_fetch_time
    rate_limit = 4000  # 4 seconds in milliseconds
    
    cache_key = f"believe-{min_followers}"
    
    if not force_refresh and time_since_last_fetch < rate_limit and cache_key in believe_token_cache:
        # print(f"Using cached Believe tokens data for min_followers={min_followers}")  # Comment out non-error log
        return believe_token_cache[cache_key]
    
    try:
        # Build the API URL with the specified minimum followers
        api_url = f"https://api.believesignal.com/tokens?count=50&min_followers={min_followers}"
        
        headers = {
            "Accept": "application/json",
            "User-Agent": "Solvirx/1.0",
        }
        
        # print(f"Fetching Believe tokens with min_followers={min_followers}")  # Comment out non-error log
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"API returned status {response.status}: {error_text[:150]}...")
                
                data = await response.json()
                
                if not data or not isinstance(data, list):
                    print("No valid data received from Believe API.")  # Keep this error log
                    return []
                
                # Process the data to match our token format
                processed_tokens = []
                
                for token in data:
                    # Convert to our standard token format
                    processed_token = {
                        "address": token.get('ca_address', ''),
                        "name": token.get('coin_name', ''),
                        "symbol": token.get('coin_ticker', ''),
                        "twitter": token.get('twitter_handler', ''),
                        "website": token.get('link', ''),
                        "created_at": token.get('created_at', ''),
                        "source": "believe",  # Mark the source
                        "original_data": token  # Keep original data for reference
                    }
                    
                    # Add Twitter info if available
                    if 'twitter_info' in token and isinstance(token['twitter_info'], dict):
                        processed_token["twitterFollowers"] = token['twitter_info'].get('followers_count', 0)
                        processed_token["twitterVerified"] = token['twitter_info'].get('is_blue_verified', False)
                    
                    processed_tokens.append(processed_token)
                
                # Cache the results
                believe_token_cache[cache_key] = processed_tokens
                last_believe_fetch_time = now
                
                print(f"Fetched {len(processed_tokens)} tokens from Believe API")  # Comment out non-error log
                return processed_tokens
    
    except Exception as e:
        print(f"Error fetching Believe tokens: {str(e)}")  # Keep this error log
        # Return cached data if available
        if cache_key in believe_token_cache:
            return believe_token_cache[cache_key]
        return []
    
    
async def add_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a new filter"""
    user_id = update.effective_user.id
    
    # Check if arguments are provided
    if not context.args or len(context.args) < 2:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="❌ Please provide filter type and value.\nExample: /add_filter believe 0"
        )
        return
    
    filter_type = context.args[0].lower()
    filter_value = ' '.join(context.args[1:])
    
    # Add "believe" to valid types
    valid_types = ["twitter", "website", "token", "wallet", "believe"]
    if filter_type not in valid_types:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"❌ Invalid filter type. Please use one of: {', '.join(valid_types)}"
        )
        return
    
    # For believe filter, validate it's a number
    if filter_type == "believe":
        try:
            min_followers = int(filter_value)
            if min_followers < 0:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ Minimum followers must be a positive number."
                )
                return
            filter_value = str(min_followers)  # Normalize to string
        except ValueError:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Invalid input for believe filter. Please provide a valid number for minimum followers."
            )
            return
    
    # Create filter
    new_filter = WatchlistFilter(
        filter_id=str(int(datetime.now().timestamp())),
        filter_type=filter_type,
        value=filter_value
    )
    
    # Check if filter already exists
    if user_id not in user_filters:
        user_filters[user_id] = []
    
    # Check for duplicates
    for existing_filter in user_filters[user_id]:
        if are_filters_equal(existing_filter, new_filter):
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="❌ This filter already exists!"
            )
            return
    
    # Add filter
    user_filters[user_id].append(new_filter)
    save_user_data(user_id)
    
    # Special message for believe filter
    if filter_type == "believe":
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"✅ Added new Believe filter with minimum {filter_value} followers.\n\nUse /start_tracking to begin monitoring for new tokens."
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"✅ Added new filter: {filter_type} - {filter_value}"
        )

async def list_filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all filters for a user"""
    user_id = update.effective_user.id
    
    if user_id not in user_filters or not user_filters[user_id]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="You don't have any filters yet. Add one with /add_filter"
        )
        return
    
    filters_text = "📋 Your current filters:\n\n"
    for i, filter_obj in enumerate(user_filters[user_id], 1):
        filters_text += f"{i}. Type: {filter_obj.type}, Value: {filter_obj.value}\n"
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=filters_text)

async def remove_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a filter by index"""
    user_id = update.effective_user.id
    
    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="❌ Please provide the index of the filter to remove.\nExample: /remove_filter 1"
        )
        return
    
    try:
        index = int(context.args[0]) - 1  # Convert to 0-based index
        if user_id not in user_filters or index < 0 or index >= len(user_filters[user_id]):
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="❌ Invalid filter index. Use /list_filters to see your filters."
            )
            return
        
        removed = user_filters[user_id].pop(index)
        save_user_data(user_id)
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"✅ Removed filter: {removed.type} - {removed.value}"
        )
    except ValueError:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="❌ Please provide a valid number."
        )

# Update the matches_command function to show all matches (no limit)
async def matches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows matched tokens with complete information"""
    user_id = update.effective_user.id
    
    if user_id not in user_matched_tokens or not user_matched_tokens[user_id]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="You don't have any matched tokens yet."
        )
        return
    
    # Calculate total number of tokens
    total_tokens = len(user_matched_tokens[user_id])
    matches_text = f"🎯 Your matched tokens ({total_tokens} total):\n\n"
    
    # Instead of limiting to 10, we'll process them all but send in multiple messages if needed
    MAX_MESSAGE_LENGTH = 4000  # Telegram has a limit of approximately 4096 characters
    current_message = matches_text
    message_count = 1
    
    for i, token in enumerate(user_matched_tokens[user_id], 1):
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        address = token.get('address', 'Unknown')
        website = token.get('website', '')
        twitter = token.get('twitter', '')
        
        token_text = f"{i}. {symbol} ({name})\n"
        token_text += f"   🔗 CA: `{address}`\n"
        
        if website:
            token_text += f"   🌐 Website: {website}\n"
        
        if twitter:
            # Format Twitter handle
            if not twitter.startswith('@') and not twitter.startswith('http'):
                twitter = f"@{twitter}"
            token_text += f"   🐦 Twitter: {twitter}\n"
        
        token_text += "\n"
        
        # Check if adding this token would exceed message length
        if len(current_message) + len(token_text) > MAX_MESSAGE_LENGTH:
            # Send current message and start a new one
            await context.bot.send_message(chat_id=update.effective_chat.id, text=current_message)
            message_count += 1
            current_message = f"🎯 Your matched tokens (part {message_count}):\n\n{token_text}"
        else:
            current_message += token_text
    
    # Send the final message if there's anything left
    if current_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=current_message)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows match history"""
    user_id = update.effective_user.id
    
    if user_id not in user_match_history or not user_match_history[user_id]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="You don't have any match history yet."
        )
        return
    
    # Calculate total entries
    total_entries = len(user_match_history[user_id])
    history_text = f"📜 Your match history ({total_entries} total):\n\n"
    
    # Instead of limiting to 10, we'll process them all but send in multiple messages if needed
    MAX_MESSAGE_LENGTH = 4000  # Telegram has a limit of approximately 4096 characters
    current_message = history_text
    message_count = 1
    
    for i, entry in enumerate(user_match_history[user_id], 1):
        token = entry.token
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        match_time = datetime.fromtimestamp(entry.timestamp/1000).strftime('%Y-%m-%d %H:%M:%S')
        filter_type = entry.filter.type
        filter_value = entry.filter.value
        
        entry_text = f"{i}. {symbol} ({name})\n"
        entry_text += f"   Matched: {match_time}\n"
        entry_text += f"   Filter: {filter_type} - {filter_value}\n\n"
        
        # Check if adding this entry would exceed message length
        if len(current_message) + len(entry_text) > MAX_MESSAGE_LENGTH:
            # Send current message and start a new one
            await context.bot.send_message(chat_id=update.effective_chat.id, text=current_message)
            message_count += 1
            current_message = f"📜 Your match history (part {message_count}):\n\n{entry_text}"
        else:
            current_message += entry_text
    
    # Send the final message if there's anything left
    if current_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=current_message)

async def clear_matches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clears all matched tokens"""
    user_id = update.effective_user.id
    
    if user_id not in user_matched_tokens or not user_matched_tokens[user_id]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="You don't have any matched tokens to clear."
        )
        return
    
    # Before clearing, ensure all matched tokens are in history
    current_time = int(datetime.now().timestamp() * 1000)
    
    if user_id not in user_match_history:
        user_match_history[user_id] = []
    
    # Get existing addresses in history
    existing_addresses = set()
    for entry in user_match_history[user_id]:
        if 'address' in entry.token:
            existing_addresses.add(entry.token['address'])
    
    # Add tokens to history if not already there
    for token in user_matched_tokens[user_id]:
        if 'address' not in token or token['address'] not in existing_addresses:
            # Find a matching filter or use a generic one
            matched_filter = None
            for filter_obj in user_filters.get(user_id, []):
                if check_token_match(token, [filter_obj]):
                    matched_filter = filter_obj
                    break
            
            if not matched_filter:
                matched_filter = WatchlistFilter(
                    filter_id=f"auto-{current_time}",
                    filter_type="token",
                    value=token.get('symbol', token.get('name', 'Unknown'))
                )
            
            user_match_history[user_id].append(
                MatchHistoryEntry(token, current_time, matched_filter)
            )
    
    # Clear matched tokens
    user_matched_tokens[user_id] = []
    save_user_data(user_id)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text="✅ All matched tokens have been cleared. They are still in your history."
    )

async def toggle_sound_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles notification sound"""
    user_id = update.effective_user.id
    
    if user_id not in user_sound_enabled:
        user_sound_enabled[user_id] = True
    
    user_sound_enabled[user_id] = not user_sound_enabled[user_id]
    save_user_data(user_id)
    
    status = "enabled" if user_sound_enabled[user_id] else "disabled"
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=f"🔊 Notification sound {status}."
    )

async def fetch_with_retry(url, headers, max_retries=3):
    """Helper function to retry failed requests with better error handling"""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            # Create a timeout for the request
            timeout = aiohttp.ClientTimeout(total=30)  # 30 second timeout
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    # Check if the response is OK
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"API returned status {response.status}: {error_text[:150]}...")
                    
                    # Parse and return JSON data
                    data = await response.json()
                    return data
        except Exception as error:
            last_error = error
            is_timeout_error = isinstance(error, asyncio.TimeoutError) or "timeout" in str(error).lower()
            
            if is_timeout_error:
                print(f"Attempt {attempt + 1}/{max_retries} timed out")
            else:
                print(f"Attempt {attempt + 1}/{max_retries} failed: {error}")
            
            # Only wait and retry if we haven't exhausted all retries
            if attempt < max_retries - 1:
                # Exponential backoff with longer delays
                base_delay = 2 ** attempt * 2  # Start with 2 seconds, then 4, 8, etc.
                jitter = 1.0 * (0.5 - random.random())  # Add randomness to avoid thundering herd
                delay = min(base_delay + jitter, 20)  # Cap at 20 seconds
                
                print(f"Waiting {round(delay, 1)} seconds before retry attempt {attempt + 2}...")
                await asyncio.sleep(delay)
    
    # If all attempts failed, log and return None
    print(f"All fetch attempts failed: {last_error}")
    return None

def cache_data(key, data, ttl_minutes=30):
    """Cache data with a TTL (time-to-live)"""
    now = int(datetime.now().timestamp() * 1000)
    token_cache[key] = {
        "data": data,
        "timestamp": now,
        "expiresAt": now + ttl_minutes * 60 * 1000
    }

def get_cached_data(key):
    """Get cached data if it exists and isn't expired"""
    cached = token_cache.get(key)
    if cached and int(datetime.now().timestamp() * 1000) < cached["expiresAt"]:
        return cached["data"]
    return None

# For fetch_latest_tokens function
async def fetch_latest_tokens(force_refresh=False):
    """Fetches latest tokens from Solana API with caching and retry logic"""
    global last_fetch_time, cached_response
    
    try:
        # Rate limiting check
        now = int(datetime.now().timestamp() * 1000)
        time_since_last_fetch = now - last_fetch_time
        rate_limit = 1500  # 1.5 seconds in milliseconds
        
        if not force_refresh and time_since_last_fetch < rate_limit and cached_response:
            # print("Using rate-limited cached response")  # Comment out non-error log
            return cached_response
        
        # API URL with parameters
        api_url = "https://data.solanatracker.io/tokens/latest"
        params = {"limit": "50"}
        
        # Add cache busting if needed
        if force_refresh:
            params["_cb"] = now
        
        # Build full URL with params
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        full_url = f"{api_url}?{query_string}"
        
        # Set headers
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": SOLANA_TRACKER_API_KEY,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        
        # print(f"Fetching tokens from API: {full_url}")  # Comment out non-error log
        # print(f"Using API Key: {SOLANA_TRACKER_API_KEY[:5]}...")  # Comment out non-error log
        
        # Try the cache first if not forcing refresh
        if not force_refresh:
            cached_data = get_cached_data("latest-tokens")
            if cached_data:
                # print("Using cached tokens data")  # Comment out non-error log
                return cached_data
        
        # Fetch with retries
        data = await fetch_with_retry(full_url, headers)
        
        # If fetch failed completely, use cache or mock data
        if data is None:
            cached_data = get_cached_data("latest-tokens")
            if cached_data:
                print("Using cached data after API failure")  # Keep this as it's a fallback notification
                return cached_data
            else:
                print("All API attempts failed, using mock data")  # Keep this error scenario log
                mock_tokens = get_mock_tokens()
                cache_data("latest-tokens", mock_tokens)  # Cache the mock data
                cached_response = mock_tokens
                last_fetch_time = now
                return mock_tokens
        
        # Process the response data - this is an array of token objects
        processed_tokens = []
        
        print(f"Received {len(data)} token entries from API")  # Comment out non-error log
        
        for item in data:
            try:
                # Skip items that don't have the right structure
                if not is_valid_token_data(item):
                    print(f"Skipping invalid token data type: {type(item)}")  # Comment out non-error log
                    continue
                
                # The actual token details are in the 'token' property
                token_data = item.get('token', {})
                if not isinstance(token_data, dict):
                    token_data = {}
                
                # For tokens without a token property, use the item itself
                if not token_data and isinstance(item, dict):
                    token_data = item
                
                # Get the first pool from pools array (if it exists)
                pool_data = {}
                pools = item.get('pools', [])
                if pools and len(pools) > 0 and isinstance(pools[0], dict):
                    pool_data = pools[0]
                
                # Extract values safely
                def extract_value(obj, key, default=''):
                    if not isinstance(obj, dict):
                        return default
                    value = obj.get(key, default)
                    return value if value is not None else default
                
                # Special handling for nested objects
                def extract_nested(obj, parent_key, child_key, default=''):
                    parent = obj.get(parent_key, {})
                    if not isinstance(parent, dict):
                        return default
                    return parent.get(child_key, default)
                
                # Build the processed token with data from both token and pool
                processed_token = {
                    "address": extract_value(token_data, 'mint') or extract_value(token_data, 'address') or extract_value(pool_data, 'tokenAddress', ''),
                    "name": extract_value(token_data, 'name', ''),
                    "symbol": extract_value(token_data, 'symbol', ''),
                    "deployer": extract_value(pool_data, 'deployer', ''),
                    "creator": extract_value(token_data, 'creator', '') or extract_value(pool_data, 'deployer', ''),
                    "website": extract_value(token_data, 'website', ''),
                    "twitter": extract_value(token_data, 'twitter', ''),
                    "twitterUsername": extract_value(token_data, 'twitterUsername', ''),
                    "image": extract_value(token_data, 'image', ''),
                    "description": extract_value(token_data, 'description', ''),
                }
                
                # Handle nested properties
                market_cap = {}
                price = {}
                txns = {}
                
                if isinstance(pool_data.get('marketCap'), dict):
                    market_cap = pool_data['marketCap']
                if isinstance(pool_data.get('price'), dict):
                    price = pool_data['price']
                if isinstance(pool_data.get('txns'), dict):
                    txns = pool_data['txns']
                elif isinstance(item.get('txns'), dict):
                    txns = item['txns']
                    
                # Add these properties safely
                processed_token["marketCap"] = extract_value(market_cap, 'usd')
                processed_token["price"] = extract_value(price, 'usd')
                processed_token["volume"] = extract_value(txns, 'volume')
                processed_token["createdAt"] = extract_value(pool_data, 'createdAt')
                
                # Only add tokens with valid addresses
                if processed_token["address"]:
                    processed_tokens.append(processed_token)
                    
                    # Debug the first few tokens
                    # if len(processed_tokens) <= 3:  # Comment out debugging section
                    #     print(f"Processed token: {processed_token['symbol']} ({processed_token['name']})")
                    #     print(f"  Address: {processed_token['address']}")
                    #     print(f"  Twitter: {processed_token['twitter']}")
                    #     print(f"  Website: {processed_token['website']}")
            except Exception as token_error:
                print(f"Error processing token: {str(token_error)}")  # Keep this error log
                continue
        
        # If no valid tokens were processed, use mock tokens
        if not processed_tokens:
            print("No valid tokens processed, using mock tokens")  # Keep this error scenario log
            return get_mock_tokens()
        
        print(f"Successfully processed {len(processed_tokens)} tokens")  # Comment out non-error log
        
        # Cache the successful results
        cache_data("latest-tokens", processed_tokens)
        cached_response = processed_tokens
        last_fetch_time = now
        
        return processed_tokens
        
    except Exception as e:
        print(f"Unexpected error in fetch_latest_tokens: {e}")  # Keep this error log
        import traceback
        traceback.print_exc()  # Keep this for error debugging
        
        # Try to use cache as first fallback
        cached_data = get_cached_data("latest-tokens")
        if cached_data:
            print("Using cached data after unexpected error")  # Keep this for error recovery notification
            return cached_data
        
        # If no cache, use mock data
        print("Using mock data as final fallback")  # Keep this for error recovery notification
        return get_mock_tokens()

def get_mock_tokens():
    """Returns mock tokens when API fails"""
    print("Using mock tokens as fallback")
    return MOCK_TOKENS

def is_valid_token_data(item):
    """Checks if an item from the API response can be processed as a token"""
    # Check if the item itself is a dictionary
    if not isinstance(item, dict):
        return False
    
    # Check if it has either token or pools property
    has_token = 'token' in item and isinstance(item['token'], dict)
    has_pools = 'pools' in item and isinstance(item['pools'], list) and len(item['pools']) > 0
    
    # Check if it has the required fields directly
    has_direct_fields = ('address' in item or 'mint' in item or 'tokenAddress' in item) and ('name' in item or 'symbol' in item)
    
    return has_token or has_pools or has_direct_fields

async def process_new_tokens(user_id: int, tokens: List[Dict]):
    """Processes new tokens against user filters"""
    if not tokens or user_id not in user_filters or not user_filters[user_id]:
        # print(f"No tokens or filters for user {user_id}")  # Comment out non-error log
        return []
    
    new_matches = []
    
    # Initialize user matched tokens if not already
    if user_id not in user_matched_tokens:
        user_matched_tokens[user_id] = []
    
    # Get existing token addresses
    existing_addresses = set()
    for token in user_matched_tokens[user_id]:
        if 'address' in token:
            existing_addresses.add(token['address'])
    
    # print(f"Processing {len(tokens)} tokens against {len(user_filters[user_id])} filters for user {user_id}")  # Comment out non-error log
    # print(f"User already has {len(existing_addresses)} matched tokens")  # Comment out non-error log
    
    match_count = 0
    for idx, token in enumerate(tokens):
        # Skip if token is already matched
        if 'address' in token and token['address'] in existing_addresses:
            # if idx < 5:  # Only print first few to avoid flooding
            #     print(f"Skipping token {token.get('symbol', 'Unknown')} (already matched)")  # Comment out non-error log
            continue
        
        # Debug: print token details for a few tokens
        # if idx < 5:  # Comment out non-error log section
        #     print(f"Checking token: {token.get('symbol', 'Unknown')} ({token.get('name', 'Unknown')})")
        #     print(f"   Address: {token.get('address', 'Unknown')}")
        #     print(f"   Twitter: {token.get('twitter', 'None')}")
        #     print(f"   Website: {token.get('website', 'None')}")
        
        matched_filter = check_token_match(token, user_filters[user_id])
        if matched_filter:
            match_count += 1
            print(f"MATCH FOUND! Token: {token.get('symbol', 'Unknown')} with filter: {matched_filter.type} - {matched_filter.value}")  # Keep this as important notification
            new_matches.append({"token": token, "filter": matched_filter})
            
            # Add to matched tokens
            user_matched_tokens[user_id].append(token)
            
            # Add to history
            if user_id not in user_match_history:
                user_match_history[user_id] = []
            
            user_match_history[user_id].append(
                MatchHistoryEntry(token, int(datetime.now().timestamp() * 1000), matched_filter)
            )
    
    # Save if there are new matches
    if new_matches:
        #print(f"Found {len(new_matches)} new matches out of {match_count} total matches")  # Keep this as important notification
        save_user_data(user_id)
    # else:
    #     print(f"No new matches found out of {len(tokens)} tokens")  # Comment out non-error log
    
    return new_matches

# Update the notify_user function to show more complete token information
async def notify_user(update: Update, matches: List[Dict]):
    """Notifies user about new token matches with complete information"""
    if not matches:
        print("No matches to notify user about")
        return
    
    print(f"Sending notification for {len(matches)} matches")
    notification = f"🚨 Found {len(matches)} new matching tokens! 🚨\n\n"
    
    for i, match in enumerate(matches[:5], 1):  # Limit to 5 in the notification
        token = match["token"]
        filter_obj = match["filter"]
        
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        address = token.get('address', 'Unknown')
        website = token.get('website', '')
        twitter = token.get('twitter', '')
        
        notification += f"{i}. {symbol} ({name})\n"
        notification += f"   🔗 CA: `{address}`\n"
        
        if website:
            notification += f"   🌐 Website: {website}\n"
        
        if twitter:
            # Ensure Twitter handle has @ prefix for clarity
            if not twitter.startswith('@') and not twitter.startswith('http'):
                twitter = f"@{twitter}"
            notification += f"   🐦 Twitter: {twitter}\n"
        
        notification += f"   Matched filter: {filter_obj.type} - {filter_obj.value}\n\n"
        
        print(f"Adding notification for {symbol} ({name})")
    
    if len(matches) > 5:
        notification += f"...and {len(matches) - 5} more. Check /matches for all."
    
    try:
        print(f"Sending notification: {notification}")
        await update.message.reply_text(notification)
        print("Notification sent successfully")
    except Exception as e:
        print(f"Error sending notification: {str(e)}")

async def tracking_task(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    """Background task for token tracking"""
    try:
        # Get user's preferred polling interval or use default 5 seconds
        polling_interval = polling_intervals.get(user_id, 5)  # default to 5 seconds
        
        # Create a tracking key for this specific chat
        tracking_key = get_tracking_key(user_id, chat_id)
        
        # Initialize a set to keep track of previously seen tokens for this specific tracking task
        # This prevents the same token from being reported multiple times in the same chat
        if not hasattr(context, 'chat_matched_tokens'):
            context.chat_matched_tokens = {}
        if tracking_key not in context.chat_matched_tokens:
            context.chat_matched_tokens[tracking_key] = set()
        
        # Send initial message to the specific chat
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Token tracking started. Checking every {polling_interval} seconds for new matches."
        )
        
        # Categorize filters by type
        believe_filters = []
        other_filters = []
        
        if user_id in user_filters:
            for filter_obj in user_filters[user_id]:
                if filter_obj.type == "believe":
                    believe_filters.append(filter_obj)
                else:
                    other_filters.append(filter_obj)
        
        while True:
            all_tokens = []
            
            # Only fetch from Solana API if there are non-believe filters
            if other_filters:
                solana_tokens = await fetch_latest_tokens(force_refresh=True)
                if solana_tokens:
                    all_tokens.extend(solana_tokens)
            
            # Fetch from Believe API if there are believe filters
            if believe_filters:
                for filter_obj in believe_filters:
                    try:
                        min_followers = int(filter_obj.value)
                        believe_tokens = await fetch_believe_tokens(min_followers=min_followers, force_refresh=True)
                        if believe_tokens:
                            all_tokens.extend(believe_tokens)
                    except Exception as e:
                        print(f"Error fetching Believe tokens for filter {filter_obj.value}: {str(e)}")
            
            # Process tokens against user filters
            matches = []
            
            # Find matches that haven't been seen in THIS chat yet
            for token in all_tokens:
                if 'address' in token and token['address'] in context.chat_matched_tokens[tracking_key]:
                    # Skip tokens we've already matched in this chat
                    continue
                
                # Check if any filter matches
                matched_filter = check_token_match(token, user_filters.get(user_id, []))
                if matched_filter:
                    # Add to global user matches - this is for the /matches command
                    if user_id not in user_matched_tokens:
                        user_matched_tokens[user_id] = []
                    
                    # Check if it's already in the global matches
                    is_new_globally = True
                    for existing_token in user_matched_tokens[user_id]:
                        if 'address' in existing_token and 'address' in token and existing_token['address'] == token['address']:
                            is_new_globally = False
                            break
                    
                    # Add to global matches if it's new
                    if is_new_globally:
                        user_matched_tokens[user_id].append(token)
                        
                        # Add to match history
                        if user_id not in user_match_history:
                            user_match_history[user_id] = []
                        
                        user_match_history[user_id].append(
                            MatchHistoryEntry(token, int(datetime.now().timestamp() * 1000), matched_filter)
                        )
                        
                        # Save user data
                        save_user_data(user_id)
                    
                    # Mark this token as seen for this specific chat
                    if 'address' in token:
                        context.chat_matched_tokens[tracking_key].add(token['address'])
                    
                    # Add to current batch of matches for notification
                    matches.append({"token": token, "filter": matched_filter})
            
            # Notify user if matches found - send to the specific chat
            if matches:
                notification = f"🚨 Found {len(matches)} new matching tokens! 🚨\n\n"
                
                for i, match in enumerate(matches[:5], 1):  # Limit to 5 in notification
                    token = match["token"]
                    filter_obj = match["filter"]
                    
                    # Determine the source
                    source = "Believe" if token.get('source') == 'believe' else "Solana"
                    
                    symbol = token.get('symbol', 'Unknown')
                    name = token.get('name', 'Unknown')
                    address = token.get('address', 'Unknown')
                    website = token.get('website', '')
                    twitter = token.get('twitter', '')
                    
                    notification += f"{i}. {symbol} ({name}) - {source}\n"
                    notification += f"   🔗 CA: `{address}`\n"
                    
                    if website:
                        notification += f"   🌐 Website: {website}\n"
                    
                    if twitter:
                        if not twitter.startswith('@') and not twitter.startswith('http'):
                            twitter = f"@{twitter}"
                        notification += f"   🐦 Twitter: {twitter}\n"
                    
                    notification += f"   Matched filter: {filter_obj.type} - {filter_obj.value}\n\n"
                
                if len(matches) > 5:
                    notification += f"...and {len(matches) - 5} more. Check /matches for all."
                
                keyboard = [[InlineKeyboardButton("View All Matches", callback_data="matches")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Send notification to the specific chat
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=notification,
                    reply_markup=reply_markup
                )
            
            # Wait for next check
            await asyncio.sleep(polling_interval)
    except asyncio.CancelledError:
        # This is expected when stopping the task
        pass
    except Exception as e:
        print(f"Error in tracking task: {e}")
        # Send error notification to the specific chat
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Token tracking error: {str(e)}"
        )

# Define a helper function to create a unique key
def get_tracking_key(user_id: int, chat_id: int) -> str:
    """Creates a unique key for user-chat combination"""
    return f"{user_id}:{chat_id}"

# Update start_tracking_command
async def start_tracking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts automatic token tracking"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Check if user has filters
    if user_id not in user_filters or not user_filters[user_id]:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ You need to add filters first. Use /add_filter to add some."
        )
        return
    
    # Create a unique key for this user-chat combination
    tracking_key = get_tracking_key(user_id, chat_id)
    
    # Check if tracking is already running in this chat
    if tracking_key in polling_tasks and not polling_tasks[tracking_key].done():
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Token tracking is already running in this chat."
        )
        return
    
    # Start tracking task specific to this chat
    task = asyncio.create_task(tracking_task(update, context, user_id, chat_id))
    polling_tasks[tracking_key] = task
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 Token tracking started. Checking for new matches."
    )

# Update stop_tracking_command
async def stop_tracking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops automatic token tracking"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Create a unique key for this user-chat combination
    tracking_key = get_tracking_key(user_id, chat_id)
    
    if tracking_key in polling_tasks and not polling_tasks[tracking_key].done():
        polling_tasks[tracking_key].cancel()
        await context.bot.send_message(
            chat_id=chat_id,
            text="🛑 Token tracking stopped in this chat."
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Token tracking is not running in this chat."
        )

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually refreshes token data"""
    user_id = update.effective_user.id
    
    # Check if we're in a group chat
    is_group = update.effective_chat.type in ['group', 'supergroup']
    # Format command with bot username if in group
    matches_cmd = f"/matches{BOT_USERNAME}" if is_group else "/matches"
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔄 Checking for new tokens..."
    )
    
    # Fetch latest tokens
    tokens = await fetch_latest_tokens(force_refresh=True)
    
    if not tokens:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Failed to fetch tokens or no tokens found."
        )
        return
    
    print(f"Fetched {len(tokens)} tokens for refresh command")
    
    # Debug: print sample tokens
    for i in range(min(3, len(tokens))):
        token = tokens[i]
        print(f"Sample token {i+1}: {token.get('symbol', 'Unknown')} ({token.get('name', 'Unknown')})")
        print(f"   Address: {token.get('address', 'Unknown')}")
        print(f"   Twitter: {token.get('twitter', 'None')}")
        print(f"   Website: {token.get('website', 'None')}")
    
    # Process tokens and find matches
    matches = await process_new_tokens(user_id, tokens)
    
    # Prepare return to menu button
    keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if matches:
        print(f"Found {len(matches)} matches in refresh command")
        try:
            await notify_user(update, matches)
            print("Notification sent via notify_user")
        except Exception as e:
            print(f"Error in notify_user: {str(e)}")
            # Fallback direct message
            try:
                notification = f"🚨 Found {len(matches)} new matching tokens! Check /matches to see them."
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=notification
                )
                print("Sent fallback notification")
            except Exception as e2:
                print(f"Error sending fallback notification: {str(e2)}")
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ No new matching tokens found."
        )

# Add this function before the main() function
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to help diagnose issues"""
    user_id = update.effective_user.id
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔍 Running diagnostics..."
    )
    
    # Fetch tokens
    tokens = await fetch_latest_tokens()
    
    if not tokens:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Failed to fetch tokens."
        )
        return
    
    # Check if user has filters
    if user_id not in user_filters or not user_filters[user_id]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ You don't have any filters to check against."
        )
        return
    
    # Print debug info
    debug_text = f"Debug Info:\n"
    debug_text += f"Fetched {len(tokens)} tokens\n\n"
    
    # Show first token for debugging
    if tokens:
        sample_token = tokens[0]
        debug_text += f"Sample Token Structure:\n"
        debug_text += f"Address: {sample_token.get('address', 'None')}\n"
        debug_text += f"Name: {sample_token.get('name', 'None')}\n"
        debug_text += f"Symbol: {sample_token.get('symbol', 'None')}\n\n"
    
    # Check for specific token addresses
    for filter_obj in user_filters[user_id]:
        if filter_obj.type == "token" and len(filter_obj.value) > 30:  # Likely an address
            target_address = filter_obj.value.lower()
            found = False
            
            for token in tokens:
                token_address = token.get('address', '').lower()
                if token_address == target_address:
                    found = True
                    debug_text += f"✅ Found your filter token in the API response!\n"
                    debug_text += f"Name: {token.get('name', 'Unknown')}\n"
                    debug_text += f"Symbol: {token.get('symbol', 'Unknown')}\n\n"
                    break
            
            if not found:
                debug_text += f"❌ Your filter token ({target_address[:8]}...{target_address[-6:]}) was NOT found in the API response.\n"
                debug_text += f"This means the address doesn't match any tokens in the API's current results.\n\n"
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=debug_text
    )

# Add this function to handle text messages
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle non-command messages sent to the bot"""
    message_type = update.message.chat.type
    text = update.message.text
    
    # Only respond in groups if the bot is mentioned
    if message_type in ['group', 'supergroup']:
        # Check if the bot's username is in the message
        if BOT_USERNAME.lower() in text.lower():
            # Extract the command part after the bot username
            command_text = text.lower().split(BOT_USERNAME.lower(), 1)[1].strip()
            
            # If no specific command/text after mention, show help
            if not command_text:
                await update.message.reply_text("Hello! Use /help to see available commands.")
                return
            
            # Process the command after the mention
            response = await get_response(command_text)
            await update.message.reply_text(response)
    elif message_type == 'private':
        # In private chats, respond to all messages
        response = await get_response(text)
        await update.message.reply_text(response)

async def get_response(text: str) -> str:
    """Generate a response to a user's message"""
    processed_text = text.lower()
    
    if 'hello' in processed_text or 'hi' in processed_text:
        return "Hello! How can I help you with token tracking? Use /help to see available commands."
    
    if 'help' in processed_text:
        return "Use the /help command to see all available options."
    
    if 'filter' in processed_text or 'track' in processed_text or 'token' in processed_text:
        return "I can track tokens for you! Use /add_filter to add filters and /start_tracking to begin monitoring."
    
    return "I'm not sure how to respond to that. Try using /help to see available commands."

# Error handler
async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors caused by updates"""
    print(f'Update {update} caused error {context.error}')

# Add search_token_command which is also referenced but not defined
async def search_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches for a specific token by address or name"""
    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="❌ Please provide a token address or name to search for.\nExample: /search_token BTC"
        )
        return
    
    search_query = ' '.join(context.args).lower()
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🔍 Searching for token: {search_query}..."
    )
    
    # Fetch tokens
    tokens = await fetch_latest_tokens()
    
    if not tokens:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Failed to fetch tokens."
        )
        return
    
    # Check for exact address match first
    exact_match = None
    for token in tokens:
        if token.get('address', '').lower() == search_query:
            exact_match = token
            break
    
    if exact_match:
        token = exact_match
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        address = token.get('address', 'Unknown')
        deployer = token.get('deployer', 'Unknown')
        website = token.get('website', 'None')
        twitter = token.get('twitter', 'None')
        
        result_text = f"🎯 Found exact address match:\n\n"
        result_text += f"Symbol: {symbol}\n"
        result_text += f"Name: {name}\n"
        result_text += f"Address: {address}\n"
        result_text += f"Deployer: {deployer}\n"
        result_text += f"Website: {website}\n"
        result_text += f"Twitter: {twitter}\n\n"
        result_text += f"To track this token, use:\n/add_filter token {address}"
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=result_text
        )
        return
    
    # If no exact match, search for partial matches
    matching_tokens = []
    for token in tokens:
        token_address = token.get('address', '').lower()
        token_name = token.get('name', '').lower()
        token_symbol = token.get('symbol', '').lower()
        
        if (search_query in token_address) or (search_query in token_name) or (search_query in token_symbol):
            matching_tokens.append(token)
    
    if not matching_tokens:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ No tokens found matching '{search_query}'."
        )
        return
    
    # Display results
    results_text = f"🔍 Found {len(matching_tokens)} tokens matching '{search_query}':\n\n"
    
    for i, token in enumerate(matching_tokens[:5], 1):
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        address = token.get('address', 'Unknown')
        
        results_text += f"{i}. {symbol} ({name})\n"
        results_text += f"   Address: {address}\n\n"
    
    if len(matching_tokens) > 5:
        results_text += f"...and {len(matching_tokens) - 5} more."
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=results_text
    )


async def believe_tracker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Default minimum followers
    min_followers = 0
    
    # Check if user provided an argument for minimum followers
    if context.args:
        try:
            min_followers = int(context.args[0])
            if min_followers < 0:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ Minimum followers must be a positive number. Using 0 as default."
                )
                min_followers = 0
        except ValueError:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Invalid input. Please provide a valid number for minimum followers."
            )
            return
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🔍 Fetching tokens of Believe with minimum {min_followers} followers..."
    )
    
    try:
        # Build the API URL with the specified minimum followers
        api_url = f"https://api.believesignal.com/tokens?count=50&min_followers={min_followers}"
        
        headers = {
            "Accept": "application/json",
            "User-Agent": "Solvirx/1.0",
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"API returned status {response.status}: {error_text[:150]}...")
                
                data = await response.json()
                
                if not data or not isinstance(data, list):
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="❌ No valid data received from Believe."
                    )
                    return
                
                # Display success message with data summary
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ Found {len(data)} tokens with {min_followers}+ followers on Believe."
                )
                
                # Process and display the tokens
                tokens_text = f"🪙 Latest Tokens (Minimum {min_followers} followers):\n\n"
                
                for i, token in enumerate(data[:10], 1):  # Limit to 10 tokens
                    try:
                        coin_name = token.get('coin_name', 'Unknown')
                        coin_ticker = token.get('coin_ticker', 'Unknown')
                        ca_address = token.get('ca_address', 'Unknown')
                        twitter_handler = token.get('twitter_handler', 'Unknown')
                        created_at = token.get('created_at', '')
                        
                        # Format the creation date
                        try:
                            created_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            formatted_date = created_time.strftime('%Y-%m-%d %H:%M:%S UTC')
                        except:
                            formatted_date = created_at
                        
                        # Get Twitter followers and verified status
                        followers_count = 0
                        is_verified = False
                        
                        if 'twitter_info' in token and isinstance(token['twitter_info'], dict):
                            twitter_info = token['twitter_info']
                            followers_count = twitter_info.get('followers_count', 0)
                            is_verified = twitter_info.get('is_blue_verified', False)
                        
                        # Add token to the message
                        tokens_text += f"{i}. {coin_name} (${coin_ticker})\n"
                        tokens_text += f"   🔗 CA: `{ca_address}`\n"
                        tokens_text += f"   🐦 Twitter: @{twitter_handler}"
                        
                        if is_verified:
                            tokens_text += " ✓"
                        
                        tokens_text += f" ({followers_count:,} followers)\n"
                        tokens_text += f"   ⏰ Created: {formatted_date}\n"
                        tokens_text += f"   To add filter: /add_filter token {coin_ticker} or /add_filter twitter {twitter_handler}\n\n"
                    except Exception as token_error:
                        print(f"Error processing token {i}: {str(token_error)}")
                        continue
                
                if len(data) > 10:
                    tokens_text += f"...and {len(data) - 10} more tokens.\n"
                
                # Add instructions
                tokens_text += "\nTo track these tokens:\n"
                def format_command_for_chat(cmd, chat_type):
                    """Format command for group or private chat."""
                    if chat_type in ['group', 'supergroup']:
                        return f"{cmd}{BOT_USERNAME}"
                    return cmd

                add_filter_cmd = format_command_for_chat("/add_filter", update.effective_chat.type)
                start_tracking_cmd = format_command_for_chat("/start_tracking", update.effective_chat.type)
                tokens_text += f"1. Add a filter with {add_filter_cmd} token <ticker> or {add_filter_cmd} twitter <handle>\n"
                tokens_text += f"2. Start tracking with {start_tracking_cmd}\n"                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=tokens_text
                )
                
    except Exception as e:
        error_message = f"❌ Error fetching Believe tokens: {str(e)}"
        print(error_message)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=error_message
        )

# Update stop_tracking_callback
async def stop_tracking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop tracking from button press"""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Create a unique key for this user-chat combination
    tracking_key = get_tracking_key(user_id, chat_id)
    
    if tracking_key in polling_tasks and not polling_tasks[tracking_key].done():
        polling_tasks[tracking_key].cancel()
        
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="🛑 Token tracking stopped in this chat.",
            reply_markup=reply_markup
        )
    else:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="⚠️ Token tracking is not running in this chat.",
            reply_markup=reply_markup
        )

async def matches_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows matched tokens via callback"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Check if we're in a group chat
    is_group = update.effective_chat.type in ['group', 'supergroup']
    # Format command with bot username if in group
    matches_cmd = f"/matches{BOT_USERNAME} " if is_group else "/matches"
    
    if user_id not in user_matched_tokens or not user_matched_tokens[user_id]:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="You don't have any matched tokens yet.",
            reply_markup=reply_markup
        )
        return
    
    # For inline buttons, limit to a shorter summary to avoid message length issues
    total_tokens = len(user_matched_tokens[user_id])
    matches_text = f"🎯 Your matched tokens ({total_tokens} total):\n\n"
    
    # Show just the first 5 tokens
    for i, token in enumerate(user_matched_tokens[user_id][:5], 1):
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        
        matches_text += f"{i}. {symbol} ({name})\n"
    
    if total_tokens > 5:
        matches_text += f"\n...and {total_tokens - 5} more tokens."
    
    matches_text += f"\n\nUse {matches_cmd} command to see full details."
    
    keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=matches_text,
        reply_markup=reply_markup
    )

async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows match history via callback"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Check if we're in a group chat
    is_group = update.effective_chat.type in ['group', 'supergroup']
    # Format command with bot username if in group
    history_cmd = f"/history{BOT_USERNAME} " if is_group else "/history"
    
    if user_id not in user_match_history or not user_match_history[user_id]:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="You don't have any match history yet.",
            reply_markup=reply_markup
        )
        return
    
    # For inline buttons, limit to a shorter summary
    total_entries = len(user_match_history[user_id])
    history_text = f"📜 Your match history ({total_entries} total):\n\n"
    
    # Show just the first 5 entries
    for i, entry in enumerate(user_match_history[user_id][:5], 1):
        token = entry.token
        symbol = token.get('symbol', 'Unknown')
        name = token.get('name', 'Unknown')
        
        history_text += f"{i}. {symbol} ({name})\n"
    
    if total_entries > 5:
        history_text += f"\n...and {total_entries - 5} more entries."
    
    history_text += f"\n\nUse {history_cmd} command to see full details."
    
    keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=history_text,
        reply_markup=reply_markup
    )
    
async def clear_matches_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clears all matched tokens via callback"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id not in user_matched_tokens or not user_matched_tokens[user_id]:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="You don't have any matched tokens to clear.",
            reply_markup=reply_markup
        )
        return
    
    # Before clearing, ensure all matched tokens are in history (same as in command)
    current_time = int(datetime.now().timestamp() * 1000)
    
    if user_id not in user_match_history:
        user_match_history[user_id] = []
    # Get existing addresses in history
    existing_addresses = set()
    for entry in user_match_history[user_id]:
        if 'address' in entry.token:
            existing_addresses.add(entry.token['address'])
    
    # Add tokens to history if not already there
    for token in user_matched_tokens[user_id]:
        if 'address' not in token or token['address'] not in existing_addresses:
            # Find a matching filter or use a generic one
            matched_filter = None
            for filter_obj in user_filters.get(user_id, []):
                if check_token_match(token, [filter_obj]):
                    matched_filter = filter_obj
                    break
            
            if not matched_filter:
                matched_filter = WatchlistFilter(
                    filter_id=f"auto-{current_time}",
                    filter_type="token",
                    value=token.get('symbol', token.get('name', 'Unknown'))
                )
            
            user_match_history[user_id].append(
                MatchHistoryEntry(token, current_time, matched_filter)
            )
    
    # Clear matched tokens
    user_matched_tokens[user_id] = []
    save_user_data(user_id)
    
    keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="✅ All matched tokens have been cleared. They are still in your history.",
        reply_markup=reply_markup
    )

async def refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually refreshes token data via callback"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Check if we're in a group chat
    is_group = update.effective_chat.type in ['group', 'supergroup']
    # Format command with bot username if in group
    matches_cmd = f"{BOT_USERNAME} /matches" if is_group else "/matches"
    
    # First tell the user we're working on it
    await query.edit_message_text(
        text="🔄 Checking for new tokens..."
    )
    
    # Collect all tokens from both APIs based on user's filters
    all_tokens = []
    
    # Categorize filters by type
    believe_filters = []
    other_filters = []
    
    if user_id in user_filters:
        for filter_obj in user_filters[user_id]:
            # Debug output to see what filter types are being found
            print(f"Found filter: type={filter_obj.type}, value={filter_obj.value}")
            if filter_obj.type == "believe":
                believe_filters.append(filter_obj)
            else:
                other_filters.append(filter_obj)
    
    print(f"Categorized filters: {len(believe_filters)} believe filters, {len(other_filters)} other filters")
    
    # Only fetch from Solana API if there are non-believe filters
    if other_filters:
        print("Fetching tokens from Solana API")
        solana_tokens = await fetch_latest_tokens(force_refresh=True)
        if solana_tokens:
            all_tokens.extend(solana_tokens)
    
    # Fetch from Believe API if there are believe filters
    if believe_filters:
        print(f"Fetching tokens from Believe API for {len(believe_filters)} filters")
        for filter_obj in believe_filters:
            try:
                min_followers = int(filter_obj.value)
                print(f"Fetching Believe tokens with min_followers={min_followers}")
                believe_tokens = await fetch_believe_tokens(min_followers=min_followers, force_refresh=True)
                if believe_tokens:
                    print(f"Received {len(believe_tokens)} tokens from Believe API")
                    all_tokens.extend(believe_tokens)
            except Exception as e:
                print(f"Error fetching Believe tokens for filter {filter_obj.value}: {str(e)}")
    
    if not all_tokens:
        keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text="❌ Failed to fetch tokens or no tokens found.",
            reply_markup=reply_markup
        )
        return
    
    # Process tokens and find matches
    matches = await process_new_tokens(user_id, all_tokens)
    
    # Prepare return to menu button
    keyboard = [[InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if matches:
        # Create a new message for the results instead of editing
        # This avoids the "Loading..." getting stuck if there are a lot of matches
        notification = f"🚨 Found {len(matches)} new matching tokens! 🚨\n\n"
        
        for i, match in enumerate(matches[:3], 1):  # Show just 3 for inline view
            token = match["token"]
            filter_obj = match["filter"]
            
            symbol = token.get('symbol', 'Unknown')
            name = token.get('name', 'Unknown')
            address = token.get('address', 'Unknown')
            website = token.get('website', '')
            twitter = token.get('twitter', '')
            
            notification += f"{i}. {symbol} ({name})\n"
            notification += f"   🔗 CA: `{address}`\n"
            
            if website:
                notification += f"   🌐 Website: {website}\n"
            
            if twitter:
                # Ensure Twitter handle has @ prefix for clarity
                if not twitter.startswith('@') and not twitter.startswith('http'):
                    twitter = f"@{twitter}"
                notification += f"   🐦 Twitter: {twitter}\n"
            
            notification += f"   Matched filter: {filter_obj.type} - {filter_obj.value}\n\n"
        
        if len(matches) > 3:
            notification += f"...and {len(matches) - 3} more. Check {matches_cmd} for all."
        
        # First update the clicked message
        await query.edit_message_text(
            text="✅ Refresh completed. See results below:",
            reply_markup=reply_markup
        )
        
        # Then send a new message with the matches
        match_keyboard = [
            [InlineKeyboardButton("View All Matches", callback_data="matches")],
            [InlineKeyboardButton("« Back to Main Menu", callback_data="main_menu")]
        ]
        match_reply_markup = InlineKeyboardMarkup(match_keyboard)
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=notification,
            reply_markup=match_reply_markup
        )
    else:
        await query.edit_message_text(
            text="✅ No new matching tokens found.",
            reply_markup=reply_markup
        )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the main menu with clickable buttons when the command /menu is issued."""
    # Reuse the main_menu function but adapt it for command context
    keyboard = [
        [InlineKeyboardButton("➕ Add Filter", callback_data="menu_add_filter")],
        [InlineKeyboardButton("📋 List Filters", callback_data="list_filters")],
        [InlineKeyboardButton("🔍 Start Tracking", callback_data="start_tracking")],
        [InlineKeyboardButton("⏹️ Stop Tracking", callback_data="stop_tracking")],
        [InlineKeyboardButton("⚡ Believe Tracker", callback_data="menu_believe")],
        [InlineKeyboardButton("🪙 Matched Tokens", callback_data="matches")],
        [InlineKeyboardButton("📜 Match History", callback_data="history")],
        [InlineKeyboardButton("🧹 Clear Matches", callback_data="clear_matches")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🤖 *Solvirx Token Tracker*\n\n"
             "CA: 61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi61KVy8wKc2mihBVacb6SwnWdGJjS3PivcgXToZ66sSWi\n\n"
             "Our Twitter: [Solvirx](https://x.com/solvirx)\n\n"
             "Choose an option from the menu below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
        
async def test_api_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tests the API connection with various endpoints"""
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔍 Testing API connection..."
    )
    
    # Test trending tokens endpoint
    try:
        api_url = "https://data.solanatracker.io/tokens/trending"
        
        headers = {
            "Accept": "application/json",
            "x-api-key": SOLANA_TRACKER_API_KEY,
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                status = response.status
                if status == 200:
                    try:
                        data = await response.json()
                        token_count = len(data) if isinstance(data, list) else len(data.get("data", [])) if isinstance(data, dict) and "data" in data else 0
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"✅ Trending tokens API: Success! Found {token_count} tokens."
                        )
                    except Exception as e:
                        response_text = await response.text()
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"⚠️ Trending tokens API returned status 200 but invalid JSON: {str(e)}\nResponse: {response_text[:100]}..."
                        )
                else:
                    response_text = await response.text()
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"❌ Trending tokens API failed with status {status}: {response_text[:100]}..."
                    )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error testing trending tokens API: {str(e)}"
        )
    
    # Test latest tokens endpoint
    try:
        api_url = "https://data.solanatracker.io/tokens/latest?limit=10"
        
        headers = {
            "Accept": "application/json",
            "x-api-key": SOLANA_TRACKER_API_KEY,
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                status = response.status
                if status == 200:
                    try:
                        data = await response.json()
                        token_count = len(data) if isinstance(data, list) else len(data.get("data", [])) if isinstance(data, dict) and "data" in data else 0                        
                        # Get the first token to show as example
                        if token_count > 0:
                            token = data[0] if isinstance(data, list) else data.get("data", [])[0] if isinstance(data, dict) and "data" in data else None
                            token_info = f"Example token: {token.get('symbol', 'Unknown')} ({token.get('name', 'Unknown')})" if token else "No token details available"
                            
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=f"✅ Latest tokens API: Success! Found {token_count} tokens.\n{token_info}"
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=f"✅ Latest tokens API: Success, but no tokens found."
                            )
                    except Exception as e:
                        response_text = await response.text()
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"⚠️ Latest tokens API returned status 200 but invalid JSON: {str(e)}\nResponse: {response_text[:100]}..."
                        )
                else:
                    response_text = await response.text()
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"❌ Latest tokens API failed with status {status}: {response_text[:100]}..."
                    )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error testing latest tokens API: {str(e)}"
        )
    
    # Provide overall summary
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔍 API test completed. If all tests failed, check your API key or network connectivity."
    )

def safe_get(obj, *keys, default=""):
    """Safely get nested values from a dict, handling non-dict values"""
    if not isinstance(obj, dict):
        return default
    
    current = obj
    for key in keys[:-1]:
        if not isinstance(current, dict):
            return default
        current = current.get(key, {})
    
    # For the last key
    if not isinstance(current, dict):
        return default
    last_key = keys[-1] if keys else ""
    return current.get(last_key, default)

# Modify the main() function to add these handlers
async def main():
    print('Starting bot...')
    app = Application.builder().token(TOKEN).build()
    
    # Commands - now with mention check for groups
    app.add_handler(CommandHandler('start', check_group_mention(start_command)))
    app.add_handler(CommandHandler('help', check_group_mention(help_command)))
    app.add_handler(CommandHandler('menu', check_group_mention(menu_command)))
    app.add_handler(CommandHandler('add_filter', check_group_mention(add_filter_command)))
    app.add_handler(CommandHandler('list_filters', check_group_mention(list_filters_command)))
    app.add_handler(CommandHandler('believe_tracker', check_group_mention(believe_tracker_command)))
    app.add_handler(CommandHandler('remove_filter', check_group_mention(remove_filter_command)))
    app.add_handler(CommandHandler('matches', check_group_mention(matches_command)))
    app.add_handler(CommandHandler('history', check_group_mention(history_command)))
    app.add_handler(CommandHandler('clear_matches', check_group_mention(clear_matches_command)))
    app.add_handler(CommandHandler('toggle_sound', check_group_mention(toggle_sound_command)))
    app.add_handler(CommandHandler('start_tracking', check_group_mention(start_tracking_command)))
    app.add_handler(CommandHandler('stop_tracking', check_group_mention(stop_tracking_command)))
    app.add_handler(CommandHandler('refresh', check_group_mention(refresh_command)))
    app.add_handler(CommandHandler('search_token', check_group_mention(search_token_command)))
    app.add_handler(CommandHandler('debug', check_group_mention(debug_command)))
    app.add_handler(CommandHandler('test_api', check_group_mention(test_api_command)))
    
    # Add this handler to process button clicks
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Update message handler to handle both regular messages and filter input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_filter_input))
    
    # Errors
    app.add_error_handler(error)
    
    # Start polling
    print('Bot running. Press Ctrl-C to stop.')  # Keep this important status message
    await app.initialize()
    await app.start()
    await app.updater.start_polling(poll_interval=3)
    
    try:
        # This keeps the program running until interrupted
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        print('Bot stopping...')  # Keep this important status message
    finally:
        # Properly shutdown the bot in the correct order
        try:
            # First stop polling
            if hasattr(app.updater, 'stop_polling'):
                await app.updater.stop_polling()
            
            # Then stop the updater
            try:
                if hasattr(app.updater, 'shutdown'):
                    await app.updater.shutdown()
            except RuntimeError:
                print("Updater shutdown failed - may already be stopped")  # Keep this error log
            
            # Finally stop the application
            await app.stop()
            await app.shutdown()
        except Exception as e:
            print(f"Error during shutdown: {e}")  # Keep this error log
        
        print('Bot stopped.')  # Keep this important status message

# Run the bot
if __name__ == '__main__':
    # Use the basic asyncio.run() which properly manages the event loop
    asyncio.run(main())


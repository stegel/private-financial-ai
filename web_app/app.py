"""
Private Financial AI - Main Flask Application

A privacy-first personal financial assistant.
All sensitive data stays local in SQLite.
"""

import os
import sys
import json
import sqlite3
import yaml
from datetime import datetime
from typing import Dict, List, Optional, Any, Generator
from flask import Flask, request, jsonify, Response, render_template, stream_with_context
from flask_cors import CORS

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import providers
from providers import (
    AnthropicProvider,
    OpenAIProvider,
    OllamaProvider,
    ClaudeCLIProvider,
)
from router import SmartRouter, QueryClassification

# Import tools
from mcp_server.tools import (
    SpendingTools, SPENDING_TOOLS,
    PortfolioTools, PORTFOLIO_TOOLS,
    PlaidTools, PLAID_TOOLS,
    CryptoTools, CRYPTO_TOOLS,
    MemoryTools, MEMORY_TOOLS,
    BudgetTools, BUDGET_TOOLS,
    VaultTools, VAULT_TOOLS,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

# Base paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT_DIR = os.path.join(BASE_DIR, 'vault')
DB_PATH = os.path.join(VAULT_DIR, 'databases', 'main.db')
SECRETS_DIR = os.path.expanduser('~/.private-financial-ai/secrets')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')

# Ensure directories exist
os.makedirs(os.path.join(VAULT_DIR, 'databases'), exist_ok=True)
os.makedirs(os.path.join(VAULT_DIR, 'documents'), exist_ok=True)
os.makedirs(SECRETS_DIR, exist_ok=True)

# =============================================================================
# FLASK APP SETUP
# =============================================================================

app = Flask(__name__)
CORS(app)

# =============================================================================
# INITIALIZE COMPONENTS
# =============================================================================

# Initialize router
router = SmartRouter(os.path.join(CONFIG_DIR, 'providers', 'providers.yaml'))

# Initialize tools
spending_tools = SpendingTools(DB_PATH)
portfolio_tools = PortfolioTools(DB_PATH)
plaid_tools = PlaidTools(DB_PATH, SECRETS_DIR)
crypto_tools = CryptoTools(DB_PATH, SECRETS_DIR)
memory_tools = MemoryTools(DB_PATH)
budget_tools = BudgetTools(DB_PATH)
vault_tools = VaultTools(DB_PATH, os.path.join(VAULT_DIR, 'documents'))

# Collect all tools
ALL_TOOLS = (
    SPENDING_TOOLS +
    PORTFOLIO_TOOLS +
    PLAID_TOOLS +
    CRYPTO_TOOLS +
    MEMORY_TOOLS +
    BUDGET_TOOLS +
    VAULT_TOOLS
)

# Tool name to handler mapping
TOOL_HANDLERS = {
    # Spending tools
    'get_spending_by_category': spending_tools.get_spending_by_category,
    'search_transactions': spending_tools.search_transactions,
    'get_monthly_cash_flow': spending_tools.get_monthly_cash_flow,
    'detect_recurring_expenses': spending_tools.detect_recurring_expenses,
    'get_deposits': spending_tools.get_deposits,

    # Portfolio tools
    'get_portfolio_summary': portfolio_tools.get_portfolio_summary,
    'get_holdings_by_account': portfolio_tools.get_holdings_by_account,
    'get_asset_allocation': portfolio_tools.get_asset_allocation,
    'get_top_holdings': portfolio_tools.get_top_holdings,
    'get_account_summary': portfolio_tools.get_account_summary,

    # Plaid tools
    'get_plaid_status': plaid_tools.get_plaid_status,
    'list_linked_accounts': plaid_tools.list_linked_accounts,
    'get_bank_balances': plaid_tools.get_bank_balances,
    'sync_transactions': plaid_tools.sync_transactions,

    # Crypto tools
    'get_crypto_holdings': crypto_tools.get_crypto_holdings,
    'get_defi_positions': crypto_tools.get_defi_positions,
    'get_bitcoin_holdings': crypto_tools.get_bitcoin_holdings,
    'sync_evm_wallets': crypto_tools.sync_evm_wallets,

    # Memory tools
    'create_entity': memory_tools.create_entity,
    'add_observation': memory_tools.add_observation,
    'create_relation': memory_tools.create_relation,
    'get_entity': memory_tools.get_entity,
    'search_memories': memory_tools.search_memories,
    'get_all_memories': memory_tools.get_all_memories,
    'delete_entity': memory_tools.delete_entity,
    'delete_observation': memory_tools.delete_observation,

    # Budget tools
    'get_budget_status': budget_tools.get_budget_status,
    'set_budget': budget_tools.set_budget,
    'list_budgets': budget_tools.list_budgets,
    'delete_budget': budget_tools.delete_budget,
    'get_spending_vs_budget_trend': budget_tools.get_spending_vs_budget_trend,

    # Vault tools
    'search_documents': vault_tools.search_documents,
    'list_documents': vault_tools.list_documents,
    'get_document': vault_tools.get_document,
    'get_expiring_documents': vault_tools.get_expiring_documents,
    'update_document': vault_tools.update_document,
    'get_document_types': vault_tools.get_document_types,
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_db():
    """Get database connection."""
    return sqlite3.connect(DB_PATH)


def load_system_prompt() -> str:
    """Load system prompt with context."""
    base_prompt = """You are a helpful personal financial assistant. You have access to tools that can query the user's financial data stored locally in a SQLite database.

You can help with:
- Analyzing spending patterns and categories
- Portfolio and investment tracking
- Budget management and tracking
- Cryptocurrency holdings
- Storing and retrieving memories about goals, family, etc.
- Managing important financial documents

Always be helpful, accurate, and respect the user's privacy. All financial data stays on their local machine.

When using tools:
- Call the appropriate tool to get data before answering questions about finances
- Present data clearly with formatting when helpful
- Offer insights and suggestions when appropriate

Be concise but thorough. Use markdown formatting for readability."""

    # Add memory context if available
    try:
        memories = memory_tools.get_all_memories()
        if memories.get('entities'):
            memory_context = "\n\nUser Context (from memory):\n"
            for entity in memories['entities'][:10]:  # Limit to avoid token bloat
                memory_context += f"- {entity['name']} ({entity['type']})"
                if entity['observations']:
                    memory_context += f": {entity['observations'][0][:100]}"
                memory_context += "\n"
            base_prompt += memory_context
    except Exception:
        pass

    # Add budget alerts if any
    try:
        status = budget_tools.get_budget_status()
        if status.get('alerts'):
            base_prompt += f"\n\nBudget Alerts:\n" + "\n".join(f"- {a}" for a in status['alerts'][:5])
    except Exception:
        pass

    return base_prompt


def execute_tool(tool_name: str, arguments: Dict) -> Any:
    """Execute a tool by name with arguments."""
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        return handler(**arguments)
    except Exception as e:
        return {"error": f"Tool error: {str(e)}"}


def log_api_usage(provider: str, model: str, tokens_in: int, tokens_out: int, cost: float):
    """Log API usage for cost tracking."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO api_usage (provider, model, tokens_in, tokens_out, cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (provider, model, tokens_in, tokens_out, cost, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


# =============================================================================
# API ROUTES - CHAT
# =============================================================================

@app.route('/api/chat', methods=['POST'])
def chat():
    """Non-streaming chat endpoint."""
    data = request.json
    message = data.get('message', '')
    conversation_id = data.get('conversation_id')

    if not message:
        return jsonify({"error": "Message required"}), 400

    try:
        # Route the query
        decision = router.route(message)
        provider = decision.provider
        model = decision.model

        # Build messages
        messages = [{"role": "user", "content": message}]

        # Get tools if needed
        tools = ALL_TOOLS if decision.classification.needs_tools else None

        # Get response
        system = load_system_prompt()
        response = provider.chat(messages, tools=tools, system=system, model=model)

        # Handle tool calls
        while response.tool_calls:
            # Execute each tool
            tool_results = []
            for tc in response.tool_calls:
                result = execute_tool(tc['name'], tc['arguments'])
                tool_results.append(provider.format_tool_result(tc['id'], result))

            # Continue conversation with tool results
            messages.append(provider.format_assistant_message(response))
            messages.extend(tool_results)

            response = provider.chat(messages, tools=tools, system=system, model=model)

        # Log usage
        log_api_usage(provider.name, model, response.tokens_in, response.tokens_out, response.cost)

        return jsonify({
            "response": response.content,
            "model": model,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "cost": response.cost
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/chat/stream', methods=['POST'])
def chat_stream():
    """Streaming chat endpoint using Server-Sent Events."""
    data = request.json
    message = data.get('message', '')
    conversation_id = data.get('conversation_id')

    if not message:
        return jsonify({"error": "Message required"}), 400

    def generate():
        try:
            # Route the query
            decision = router.route(message)
            provider = decision.provider
            model = decision.model

            yield f"data: {json.dumps({'type': 'model', 'model': model})}\n\n"

            # Build messages
            messages = [{"role": "user", "content": message}]

            # Get tools if needed
            tools = ALL_TOOLS if decision.classification.needs_tools else None

            # Get system prompt
            system = load_system_prompt()

            # Stream response
            full_response = ""
            tool_calls = []

            for chunk in provider.chat(messages, tools=tools, system=system, model=model, stream=True):
                if chunk.get('type') == 'text':
                    full_response += chunk['content']
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk['content']})}\n\n"
                elif chunk.get('type') == 'tool_start':
                    yield f"data: {json.dumps({'type': 'tool_start', 'name': chunk.get('name')})}\n\n"
                elif chunk.get('type') == 'tool_call':
                    tool_calls.append(chunk)
                elif chunk.get('type') == 'done':
                    break

            # Handle tool calls if any
            if tool_calls:
                for tc in tool_calls:
                    result = execute_tool(tc['name'], tc.get('arguments', {}))
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': tc['name'], 'result': result})}\n\n"

                # Continue with tool results (simplified - full implementation would loop)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


# =============================================================================
# API ROUTES - WIDGETS & DATA
# =============================================================================

@app.route('/api/widgets/summary', methods=['GET'])
def get_widget_summary():
    """Get dashboard widget data."""
    try:
        # Portfolio
        portfolio = portfolio_tools.get_portfolio_summary()

        # Bank balances
        try:
            bank = plaid_tools.get_bank_balances()
            summary = bank.get('summary', {})
            checking = summary.get('checking', 0) + summary.get('savings', 0)
        except Exception:
            checking = 0

        # Crypto
        try:
            crypto = crypto_tools.get_crypto_holdings()
            crypto_total = crypto.get('total_value', 0)
        except Exception:
            crypto_total = 0

        return jsonify({
            "portfolio": {
                "value": portfolio.get('total_value', 0),
                "last_updated": datetime.now().isoformat()
            },
            "checking": {
                "value": checking,
                "last_updated": datetime.now().isoformat()
            },
            "liquid": {
                "value": checking + crypto_total,
                "crypto": crypto_total,
                "last_updated": datetime.now().isoformat()
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/model', methods=['GET'])
def get_model():
    """Get current model setting."""
    return jsonify({
        "model": "auto",
        "available_providers": router.get_available_providers()
    })


@app.route('/api/model', methods=['POST'])
def set_model():
    """Set model preference."""
    data = request.json
    model = data.get('model', 'auto')
    # Store preference (simplified - would persist to config)
    return jsonify({"success": True, "model": model})


# =============================================================================
# API ROUTES - CONVERSATIONS
# =============================================================================

@app.route('/api/conversations', methods=['GET'])
def list_conversations():
    """List conversations."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.conversation_id, c.title, c.created_at, c.updated_at,
               (SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = c.conversation_id) as message_count
        FROM conversations c
        ORDER BY c.updated_at DESC
        LIMIT 50
    """)

    conversations = []
    for row in cursor.fetchall():
        if row[4] > 0:  # Only include conversations with messages
            conversations.append({
                "conversation_id": row[0],
                "title": row[1],
                "created_at": row[2],
                "updated_at": row[3],
                "message_count": row[4]
            })

    conn.close()
    return jsonify({"conversations": conversations})


@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    """Create a new conversation."""
    import uuid
    conversation_id = str(uuid.uuid4())

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO conversations (conversation_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?)
    """, (conversation_id, "New Conversation", datetime.now().isoformat(), datetime.now().isoformat()))

    conn.commit()
    conn.close()

    return jsonify({"conversation_id": conversation_id})


@app.route('/api/conversations/<conversation_id>', methods=['GET'])
def get_conversation(conversation_id):
    """Get conversation with messages."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT conversation_id, title, created_at, updated_at
        FROM conversations WHERE conversation_id = ?
    """, (conversation_id,))

    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Conversation not found"}), 404

    conversation = {
        "conversation_id": row[0],
        "title": row[1],
        "created_at": row[2],
        "updated_at": row[3],
        "messages": []
    }

    cursor.execute("""
        SELECT role, content, model, created_at
        FROM conversation_messages
        WHERE conversation_id = ?
        ORDER BY created_at
    """, (conversation_id,))

    for msg in cursor.fetchall():
        conversation["messages"].append({
            "role": msg[0],
            "content": msg[1],
            "model": msg[2],
            "created_at": msg[3]
        })

    conn.close()
    return jsonify(conversation)


@app.route('/api/conversations/<conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id):
    """Delete a conversation and its messages."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM conversation_messages WHERE conversation_id = ?", (conversation_id,))
    cursor.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/conversations/<conversation_id>', methods=['PATCH'])
def update_conversation(conversation_id):
    """Update conversation title."""
    data = request.json
    title = (data.get('title') or '').strip()[:120]
    if not title:
        return jsonify({"error": "title required"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE conversation_id = ?",
        (title, datetime.now().isoformat(), conversation_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/conversations/<conversation_id>/messages', methods=['POST'])
def save_message(conversation_id):
    """Save a message to conversation."""
    data = request.json

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO conversation_messages
        (conversation_id, role, content, model, tokens_in, tokens_out, cost, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        conversation_id,
        data.get('role', 'user'),
        data.get('content', ''),
        data.get('model'),
        data.get('tokens_in'),
        data.get('tokens_out'),
        data.get('cost'),
        datetime.now().isoformat()
    ))

    # Update conversation timestamp
    cursor.execute("""
        UPDATE conversations SET updated_at = ? WHERE conversation_id = ?
    """, (datetime.now().isoformat(), conversation_id))

    conn.commit()
    conn.close()

    return jsonify({"success": True})


# =============================================================================
# API ROUTES - SYNC
# =============================================================================

@app.route('/api/plaid/sync', methods=['POST'])
def sync_plaid():
    """Sync transactions from Plaid."""
    result = plaid_tools.sync_transactions()
    return jsonify(result)


@app.route('/api/plaid/status', methods=['GET'])
def plaid_status():
    """Get Plaid integration status."""
    return jsonify(plaid_tools.get_plaid_status())


@app.route('/api/plaid/create-link-token', methods=['POST'])
def create_link_token():
    """Create a Plaid Link token for the frontend."""
    result = plaid_tools.create_link_token()
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/plaid/exchange-token', methods=['POST'])
def exchange_plaid_token():
    """Exchange a Plaid public token for an access token."""
    data = request.json
    public_token = data.get('public_token')
    metadata = data.get('metadata', {})

    if not public_token:
        return jsonify({"error": "public_token required"}), 400

    result = plaid_tools.exchange_public_token(public_token, metadata)
    if not result.get('success'):
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/plaid/items/<item_id>', methods=['DELETE'])
def remove_plaid_item(item_id):
    """Remove a Plaid bank connection."""
    result = plaid_tools.remove_item(item_id)
    if not result.get('success'):
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/settings/plaid-credentials', methods=['GET'])
def get_plaid_credentials():
    """Return Plaid credential status (never the secrets themselves)."""
    config_path = os.path.join(SECRETS_DIR, 'plaid.conf')
    if not os.path.exists(config_path):
        return jsonify({"configured": False, "env": "sandbox"})

    config = {}
    try:
        with open(config_path, 'r') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()
    except Exception:
        return jsonify({"configured": False, "env": "sandbox"})

    client_id = config.get('PLAID_CLIENT_ID', '')
    env = config.get('PLAID_ENV', 'sandbox')
    return jsonify({
        "configured": bool(client_id),
        "client_ready": plaid_tools.is_available(),
        "client_id_hint": (client_id[:4] + '...' + client_id[-4:]) if len(client_id) > 8 else client_id,
        "env": env
    })


@app.route('/api/settings/plaid-credentials', methods=['POST'])
def save_plaid_credentials():
    """Save Plaid credentials to secrets file."""
    data = request.json
    client_id = (data.get('client_id') or '').strip()
    secret = (data.get('secret') or '').strip()
    env = (data.get('env') or 'sandbox').strip()

    if not client_id or not secret:
        return jsonify({"error": "client_id and secret are required"}), 400

    if env not in ('sandbox', 'development', 'production'):
        return jsonify({"error": "env must be sandbox, development, or production"}), 400

    config_path = os.path.join(SECRETS_DIR, 'plaid.conf')
    try:
        with open(config_path, 'w') as f:
            f.write(f"PLAID_CLIENT_ID={client_id}\n")
            f.write(f"PLAID_SECRET={secret}\n")
            f.write(f"PLAID_ENV={env}\n")
        os.chmod(config_path, 0o600)
    except Exception as e:
        return jsonify({"error": f"Failed to save credentials: {str(e)}"}), 500

    # Re-initialize the Plaid client with new credentials
    plaid_tools._init_client()

    return jsonify({"success": True})


@app.route('/api/crypto/sync', methods=['POST'])
def sync_crypto():
    """Sync crypto wallet balances."""
    result = crypto_tools.sync_evm_wallets()
    return jsonify(result)


# =============================================================================
# API ROUTES - USAGE STATS
# =============================================================================

@app.route('/api/usage/stats', methods=['GET'])
def usage_stats():
    """Get API usage statistics."""
    conn = get_db()
    cursor = conn.cursor()

    # Today's usage
    cursor.execute("""
        SELECT provider, SUM(tokens_in), SUM(tokens_out), SUM(cost)
        FROM api_usage
        WHERE date(created_at) = date('now')
        GROUP BY provider
    """)

    today = {}
    for row in cursor.fetchall():
        today[row[0]] = {
            "tokens_in": row[1],
            "tokens_out": row[2],
            "cost": round(row[3], 4)
        }

    # This month's usage
    cursor.execute("""
        SELECT SUM(cost) FROM api_usage
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
    """)

    monthly_cost = cursor.fetchone()[0] or 0

    conn.close()

    return jsonify({
        "today": today,
        "monthly_cost": round(monthly_cost, 2)
    })


# =============================================================================
# UI ROUTES
# =============================================================================

@app.route('/')
def index():
    """Main chat interface."""
    return render_template('index.html')


@app.route('/upload')
def upload():
    """File upload page."""
    return render_template('upload.html')


@app.route('/budgets')
def budgets():
    """Budget management page."""
    return render_template('budgets.html')


@app.route('/vault')
def vault():
    """Document vault page."""
    return render_template('vault.html')


@app.route('/settings')
def settings():
    """Settings page."""
    return render_template('settings.html')


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    # Initialize database if needed
    schema_path = os.path.join(BASE_DIR, 'database', 'schema.sql')
    if os.path.exists(schema_path) and not os.path.exists(DB_PATH):
        print("Initializing database...")
        conn = sqlite3.connect(DB_PATH)
        with open(schema_path, 'r') as f:
            conn.executescript(f.read())
        conn.close()
        print("Database initialized.")

    print(f"Starting Private Financial AI...")
    print(f"Database: {DB_PATH}")
    print(f"Available providers: {router.get_available_providers()}")
    print(f"Access at: http://localhost:5001")

    app.run(host='127.0.0.1', port=5001, debug=True)

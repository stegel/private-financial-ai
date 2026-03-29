"""
Plaid integration tools.
Handles bank account connections and transaction syncing.
"""

import os
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any


class PlaidTools:
    """Tools for Plaid bank connections."""

    def __init__(self, db_path: str, secrets_path: Optional[str] = None):
        self.db_path = db_path
        self.secrets_path = secrets_path or os.path.expanduser('~/.private-financial-ai/secrets')
        self.client = None
        self._init_client()

    def _init_client(self):
        """Initialize Plaid client from config."""
        config_path = os.path.join(self.secrets_path, 'plaid.conf')
        if not os.path.exists(config_path):
            return

        config = {}
        try:
            with open(config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        key, value = line.split('=', 1)
                        config[key.strip()] = value.strip()
        except Exception:
            return

        client_id = config.get('PLAID_CLIENT_ID')
        secret = config.get('PLAID_SECRET')
        env = config.get('PLAID_ENV', 'development')

        if not client_id or not secret:
            return

        try:
            import plaid
            from plaid.api import plaid_api
            from plaid.configuration import Configuration

            # Plaid SDK v9+ removed Environment.Development; development
            # accounts now use the Production endpoint.
            env_map = {
                'sandbox': plaid.Environment.Sandbox,
                'development': plaid.Environment.Production,
                'production': plaid.Environment.Production,
            }

            configuration = Configuration(
                host=env_map.get(env, plaid.Environment.Production),
                api_key={
                    'clientId': client_id,
                    'secret': secret,
                }
            )

            api_client = plaid.ApiClient(configuration)
            self.client = plaid_api.PlaidApi(api_client)
        except ImportError:
            pass
        except Exception:
            pass

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def is_available(self) -> bool:
        """Check if Plaid is configured."""
        return self.client is not None

    def get_plaid_status(self) -> Dict[str, Any]:
        """
        Get Plaid integration status.

        Returns:
            Dict with status and connected institutions
        """
        if not self.client:
            return {
                "status": "not_configured",
                "message": "Plaid credentials not found. See docs/PLAID_SETUP.md"
            }

        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                item_id,
                institution_name,
                status,
                updated_at
            FROM plaid_items
            WHERE status != 'removed'
        """)

        items = []
        for row in cursor.fetchall():
            items.append({
                "item_id": row[0],
                "institution": row[1],
                "status": row[2],
                "last_updated": row[3]
            })

        conn.close()

        return {
            "status": "configured",
            "connected_institutions": len(items),
            "items": items
        }

    def list_linked_accounts(self) -> Dict[str, Any]:
        """
        List all accounts linked via Plaid.

        Returns:
            Dict with account details
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                a.account_id,
                a.name,
                a.type,
                a.institution,
                a.current_balance,
                a.mask
            FROM accounts a
            JOIN plaid_accounts pa ON a.account_id = pa.account_id
            WHERE a.is_active = 1
            ORDER BY a.institution, a.name
        """)

        accounts = []
        for row in cursor.fetchall():
            accounts.append({
                "account_id": row[0],
                "name": row[1],
                "type": row[2],
                "institution": row[3],
                "balance": row[4],
                "mask": row[5]
            })

        conn.close()

        return {
            "accounts": accounts,
            "count": len(accounts)
        }

    def get_bank_balances(self) -> Dict[str, Any]:
        """
        Get current balances from all linked accounts.

        Returns:
            Dict with account balances
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                a.name,
                a.type,
                a.institution,
                a.current_balance,
                a.available_balance,
                a.credit_limit,
                a.updated_at
            FROM accounts a
            WHERE a.is_active = 1
            ORDER BY a.type, a.current_balance DESC
        """)

        accounts = []
        total_checking = 0
        total_savings = 0
        total_credit = 0
        total_available = 0

        for row in cursor.fetchall():
            account_type = row[1] or ''
            balance = row[3] or 0

            accounts.append({
                "name": row[0],
                "type": account_type,
                "institution": row[2],
                "balance": balance,
                "available": row[4],
                "credit_limit": row[5],
                "last_updated": row[6]
            })

            if 'checking' in account_type.lower():
                total_checking += balance
                total_available += (row[4] or balance)
            elif 'saving' in account_type.lower():
                total_savings += balance
            elif 'credit' in account_type.lower():
                total_credit += balance

        conn.close()

        return {
            "accounts": accounts,
            "summary": {
                "checking": round(total_checking, 2),
                "savings": round(total_savings, 2),
                "credit_used": round(abs(total_credit), 2),
                "total_available": round(total_available, 2)
            }
        }

    def create_link_token(self, user_id: str = "user") -> Dict[str, Any]:
        """
        Create a Plaid Link token for the frontend to open Plaid Link.

        Returns:
            Dict with link_token or error
        """
        if not self.client:
            return {"error": "Plaid not configured. Add credentials first."}

        try:
            from plaid.model.link_token_create_request import LinkTokenCreateRequest
            from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
            from plaid.model.products import Products
            from plaid.model.country_code import CountryCode

            request = LinkTokenCreateRequest(
                user=LinkTokenCreateRequestUser(client_user_id=user_id),
                client_name="Private Financial AI",
                products=[Products("transactions"), Products("investments")],
                country_codes=[CountryCode("US")],
                language="en"
            )

            response = self.client.link_token_create(request)
            return {"link_token": response.link_token}

        except Exception as e:
            return {"error": str(e)}

    def exchange_public_token(self, public_token: str, metadata: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Exchange a Plaid public token for an access token and store in DB.

        Returns:
            Dict with success, item_id, institution_name
        """
        if not self.client:
            return {"success": False, "error": "Plaid not configured"}

        try:
            from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

            request = ItemPublicTokenExchangeRequest(public_token=public_token)
            response = self.client.item_public_token_exchange(request)

            access_token = response.access_token
            item_id = response.item_id

            institution_id = None
            institution_name = None
            if metadata:
                institution = metadata.get('institution', {})
                institution_id = institution.get('institution_id')
                institution_name = institution.get('name')

            conn = self._get_conn()
            cursor = conn.cursor()

            cursor.execute("""
                INSERT OR REPLACE INTO plaid_items
                (item_id, access_token, institution_id, institution_name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
            """, (
                item_id, access_token, institution_id, institution_name,
                datetime.now().isoformat(), datetime.now().isoformat()
            ))

            conn.commit()
            conn.close()

            # Immediately fetch and store account details + balances
            try:
                self._sync_accounts_for_item(item_id, access_token, institution_name or "Bank")
            except Exception:
                pass  # Non-fatal; will succeed on next sync

            # Fetch investment holdings if available
            try:
                self._sync_investments_for_item(item_id, access_token, institution_name or "Bank")
            except Exception:
                pass

            return {
                "success": True,
                "item_id": item_id,
                "institution_name": institution_name or "Bank"
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def remove_item(self, item_id: str) -> Dict[str, Any]:
        """
        Remove a Plaid item (bank connection).

        Returns:
            Dict with success or error
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT access_token FROM plaid_items WHERE item_id = ?", (item_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return {"success": False, "error": "Item not found"}

        access_token = row[0]

        if self.client:
            try:
                from plaid.model.item_remove_request import ItemRemoveRequest
                request = ItemRemoveRequest(access_token=access_token)
                self.client.item_remove(request)
            except Exception:
                pass  # Mark removed locally even if API call fails

        cursor.execute("""
            UPDATE plaid_items SET status = 'removed', updated_at = ?
            WHERE item_id = ?
        """, (datetime.now().isoformat(), item_id))

        conn.commit()
        conn.close()

        return {"success": True}

    def _sync_accounts_for_item(self, item_id: str, access_token: str, institution_name: str) -> None:
        """
        Fetch accounts from Plaid and upsert into plaid_accounts + accounts tables.
        Called after linking and during transaction sync to keep balances fresh.
        """
        from plaid.model.accounts_get_request import AccountsGetRequest

        request = AccountsGetRequest(access_token=access_token)
        response = self.client.accounts_get(request)

        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        for acct in response.accounts:
            plaid_account_id = acct.account_id
            local_account_id = f"plaid_{plaid_account_id}"
            name = acct.name
            official_name = acct.official_name
            acct_type = acct.type.value if hasattr(acct.type, 'value') else str(acct.type)
            subtype = acct.subtype.value if acct.subtype and hasattr(acct.subtype, 'value') else str(acct.subtype or '')
            mask = acct.mask

            current_balance = None
            available_balance = None
            credit_limit = None
            if acct.balances:
                current_balance = acct.balances.current
                available_balance = acct.balances.available
                credit_limit = acct.balances.limit

            # Upsert into accounts table
            cursor.execute("""
                INSERT INTO accounts
                    (account_id, name, type, institution, mask,
                     current_balance, available_balance, credit_limit,
                     is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    name=excluded.name,
                    type=excluded.type,
                    institution=excluded.institution,
                    mask=excluded.mask,
                    current_balance=excluded.current_balance,
                    available_balance=excluded.available_balance,
                    credit_limit=excluded.credit_limit,
                    is_active=1,
                    updated_at=excluded.updated_at
            """, (
                local_account_id, name, subtype or acct_type, institution_name,
                mask, current_balance, available_balance, credit_limit, now, now
            ))

            # Upsert into plaid_accounts table
            cursor.execute("""
                INSERT INTO plaid_accounts
                    (plaid_account_id, item_id, account_id, name, official_name,
                     type, subtype, mask, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plaid_account_id) DO UPDATE SET
                    account_id=excluded.account_id,
                    name=excluded.name,
                    official_name=excluded.official_name,
                    type=excluded.type,
                    subtype=excluded.subtype,
                    mask=excluded.mask
            """, (
                plaid_account_id, item_id, local_account_id, name, official_name,
                acct_type, subtype, mask, now
            ))

        conn.commit()
        conn.close()

    def _sync_investments_for_item(self, item_id: str, access_token: str, institution_name: str) -> int:
        """
        Fetch investment holdings from Plaid and upsert into investment_accounts + holdings.
        Returns number of holdings synced.
        """
        from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

        request = InvestmentsHoldingsGetRequest(access_token=access_token)
        response = self.client.investments_holdings_get(request)

        if not response.holdings:
            return 0

        # Build security lookup: security_id -> security info
        securities = {s.security_id: s for s in (response.securities or [])}

        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        # Group holdings by account
        holdings_by_account: Dict[str, list] = {}
        for holding in response.holdings:
            holdings_by_account.setdefault(holding.account_id, []).append(holding)

        # Build account lookup from the accounts on the response
        account_map = {a.account_id: a for a in (response.accounts or [])}

        total = 0
        for plaid_account_id, holdings in holdings_by_account.items():
            local_account_id = f"plaid_{plaid_account_id}"
            acct = account_map.get(plaid_account_id)
            account_name = acct.name if acct else "Investment Account"
            subtype = ""
            if acct and acct.subtype:
                subtype = acct.subtype.value if hasattr(acct.subtype, 'value') else str(acct.subtype)

            # Upsert investment_account
            cursor.execute("""
                INSERT INTO investment_accounts
                    (account_id, account_name, institution, account_type, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    account_name=excluded.account_name,
                    institution=excluded.institution,
                    account_type=excluded.account_type,
                    is_active=1,
                    updated_at=excluded.updated_at
            """, (local_account_id, account_name, institution_name, subtype or "Brokerage", now, now))

            # Mark existing holdings inactive before replacing
            cursor.execute(
                "UPDATE holdings SET is_active = 0 WHERE account_id = ?",
                (local_account_id,)
            )

            for holding in holdings:
                sec = securities.get(holding.security_id)
                symbol = sec.ticker_symbol if sec else None
                name = sec.name if sec else holding.security_id
                asset_type = sec.type.value if sec and hasattr(sec.type, 'value') else (str(sec.type) if sec else "other")

                quantity = holding.quantity
                price = holding.institution_price
                current_value = holding.institution_value
                cost_basis = holding.cost_basis

                cursor.execute("""
                    INSERT INTO holdings
                        (account_id, symbol, name, quantity, price, current_value,
                         cost_basis, asset_type, is_active, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    local_account_id, symbol, name, quantity, price,
                    current_value, cost_basis, asset_type, now
                ))
                total += 1

        conn.commit()
        conn.close()
        return total

    def _sync_investment_transactions_for_item(self, item_id: str, access_token: str) -> int:
        """
        Fetch investment transactions (trades, dividends, etc.) from Plaid
        and store them in the transactions table.
        Returns number of new transactions added.
        """
        from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
        from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions
        import datetime as dt

        start_date = dt.date.today() - dt.timedelta(days=730)
        end_date = dt.date.today()

        options = InvestmentsTransactionsGetRequestOptions(count=500, offset=0)
        request = InvestmentsTransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=options
        )
        response = self.client.investments_transactions_get(request)

        securities = {s.security_id: s for s in (response.securities or [])}

        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        total = 0

        for txn in (response.investment_transactions or []):
            txn_id = f"plaid_inv_{txn.investment_transaction_id}"
            sec = securities.get(txn.security_id)
            symbol = sec.ticker_symbol if sec else None
            name = sec.name if sec else txn.name

            description = txn.name
            if symbol:
                description = f"{txn.type.value if hasattr(txn.type, 'value') else txn.type}: {symbol} — {name}"

            cursor.execute("""
                INSERT OR IGNORE INTO transactions
                    (transaction_id, account_id, date, amount, description,
                     category, source_type, plaid_transaction_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'plaid_investment', ?, ?)
            """, (
                txn_id,
                f"plaid_{txn.account_id}",
                txn.date.isoformat(),
                -txn.amount,
                description,
                txn.subtype.value if hasattr(txn.subtype, 'value') else str(txn.subtype),
                txn.investment_transaction_id,
                now
            ))
            if cursor.rowcount:
                total += 1

        conn.commit()
        conn.close()
        return total

    def sync_transactions(self) -> Dict[str, Any]:
        """
        Sync transactions from all linked Plaid accounts.

        Returns:
            Dict with sync results
        """
        if not self.client:
            return {
                "success": False,
                "error": "Plaid not configured"
            }

        conn = self._get_conn()
        cursor = conn.cursor()

        # Get all active items
        cursor.execute("SELECT item_id, access_token FROM plaid_items WHERE status = 'active'")
        items = cursor.fetchall()

        total_new = 0
        total_modified = 0
        errors = []

        try:
            from plaid.model.transactions_sync_request import TransactionsSyncRequest

            for item_id, access_token in items:
                try:
                    # Refresh account details and balances first
                    cursor.execute(
                        "SELECT institution_name FROM plaid_items WHERE item_id = ?",
                        (item_id,)
                    )
                    inst_row = cursor.fetchone()
                    institution_name = inst_row[0] if inst_row else "Bank"
                    try:
                        self._sync_accounts_for_item(item_id, access_token, institution_name)
                    except Exception as e:
                        errors.append(f"Account sync failed for {item_id}: {str(e)}")

                    try:
                        self._sync_investments_for_item(item_id, access_token, institution_name)
                    except Exception:
                        pass  # Item may not have investments product — that's fine

                    try:
                        inv_txns = self._sync_investment_transactions_for_item(item_id, access_token)
                        total_new += inv_txns
                    except Exception:
                        pass  # Item may not have investments product — that's fine

                    # Get cursor for incremental sync
                    cursor.execute(
                        "SELECT sync_cursor FROM plaid_items WHERE item_id = ?",
                        (item_id,)
                    )
                    row = cursor.fetchone()
                    sync_cursor = row[0] if row and row[0] else ""

                    # Sync transactions
                    if sync_cursor:
                        request = TransactionsSyncRequest(
                            access_token=access_token,
                            cursor=sync_cursor
                        )
                    else:
                        request = TransactionsSyncRequest(
                            access_token=access_token
                        )

                    response = self.client.transactions_sync(request)

                    # Process new transactions
                    for txn in response.added:
                        txn_id = f"plaid_{txn.transaction_id}"
                        amount = -txn.amount  # Plaid uses positive for expenses

                        cursor.execute("""
                            INSERT OR IGNORE INTO transactions
                            (transaction_id, account_id, date, amount, description,
                             merchant_name, category, source_type, plaid_transaction_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'plaid', ?)
                        """, (
                            txn_id,
                            f"plaid_{txn.account_id}",
                            txn.date.isoformat(),
                            amount,
                            txn.name,
                            txn.merchant_name,
                            txn.personal_finance_category.primary if txn.personal_finance_category else None,
                            txn.transaction_id
                        ))
                        total_new += 1

                    # Update cursor
                    cursor.execute(
                        "UPDATE plaid_items SET sync_cursor = ?, updated_at = ? WHERE item_id = ?",
                        (response.next_cursor, datetime.now().isoformat(), item_id)
                    )

                except Exception as e:
                    errors.append(f"Error syncing item {item_id}: {str(e)}")

            conn.commit()

        except ImportError:
            return {
                "success": False,
                "error": "Plaid library not installed"
            }
        finally:
            conn.close()

        return {
            "success": True,
            "new_transactions": total_new,
            "modified_transactions": total_modified,
            "errors": errors if errors else None
        }


# Tool definitions for LLM
PLAID_TOOLS = [
    {
        "name": "get_plaid_status",
        "description": "Get Plaid integration status including connected institutions and sync activity.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "list_linked_accounts",
        "description": "List all bank accounts linked via Plaid with status and last sync time.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_bank_balances",
        "description": "Get current balances from all linked bank accounts (checking, savings, credit cards).",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "sync_transactions",
        "description": "Sync transactions from all linked Plaid accounts. Returns count of new and modified transactions.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
]

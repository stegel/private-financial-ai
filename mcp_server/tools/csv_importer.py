"""
CSV importer for financial data.
Handles Fidelity transaction history and positions exports,
plus a generic fallback for standard CSV formats.
"""

import csv
import hashlib
import io
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple


class CSVImporter:

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    # =========================================================================
    # Public entry point
    # =========================================================================

    def import_file(self, filename: str, content: str) -> Dict[str, Any]:
        """
        Detect file type and import accordingly.

        Returns:
            Dict with success, rows_imported, skipped, source, errors
        """
        fmt, file_type = self._detect_format(filename, content)

        if fmt == 'fidelity_transactions':
            return self._import_fidelity_transactions(content)
        elif fmt == 'fidelity_positions':
            return self._import_fidelity_positions(content)
        elif fmt == 'generic':
            return self._import_generic_transactions(content, filename)
        else:
            return {
                "success": False,
                "error": f"Unrecognised file format. Expected Fidelity export or a CSV with Date/Amount columns."
            }

    # =========================================================================
    # Format detection
    # =========================================================================

    def _detect_format(self, filename: str, content: str) -> Tuple[str, str]:
        """Return (format_key, description)."""
        first_line = content.split('\n')[0].lower()
        first = content[:2000].lower()

        # Fidelity positions: first line contains account number + symbol + description
        if ('account number' in first_line and 'symbol' in first_line
                and 'description' in first_line and 'quantity' in first_line):
            return 'fidelity_positions', 'Fidelity Positions'

        # Fidelity transaction history: first line contains "run date" and "action"
        if 'run date' in first_line and 'action' in first_line:
            return 'fidelity_transactions', 'Fidelity Transaction History'

        # Generic fallback
        if any(h in first for h in ['date', 'amount', 'description', 'transaction']):
            return 'generic', 'Generic CSV'

        return 'unknown', 'Unknown'

    # =========================================================================
    # Fidelity transaction history
    # =========================================================================

    def _import_fidelity_transactions(self, content: str) -> Dict[str, Any]:
        """
        Parse Fidelity's transaction CSV export.

        Fidelity format:
          - First N lines: account metadata (account name/number, date range)
          - Then a blank line
          - Then the actual CSV starting with header row:
            Run Date, Action, Symbol, Security Description, Security Type,
            Quantity, Price ($), Commission ($), Fees ($), Accrued Interest ($),
            Amount ($), Settlement Date
          - Ends with blank lines and a disclaimer footer
        """
        lines = content.splitlines()

        # Find the header row
        header_idx = None
        account_name = None
        account_number = None

        for i, line in enumerate(lines):
            # Extract account info from the preamble
            if line.startswith('Account Name'):
                parts = line.split(',')
                if len(parts) >= 2:
                    account_name = parts[1].strip().strip('"')
            if line.startswith('Account Number'):
                parts = line.split(',')
                if len(parts) >= 2:
                    account_number = parts[1].strip().strip('"')

            # The header row starts with "Run Date"
            if re.match(r'^"?Run Date"?', line, re.IGNORECASE):
                header_idx = i
                break

        if header_idx is None:
            return {"success": False, "error": "Could not find Fidelity transaction header row (Run Date, Action, ...)."}

        institution = "Fidelity"
        if account_name:
            institution = f"Fidelity — {account_name}"
        account_id = f"fidelity_{account_number or account_name or 'unknown'}"

        # Ensure account exists
        self._upsert_account(account_id, account_name or "Fidelity Account", "brokerage", institution)

        # Parse CSV from header row onward, stopping at blank/footer lines
        data_lines = []
        for line in lines[header_idx:]:
            stripped = line.strip().strip('"')
            if stripped == '' or stripped.startswith('Brokerage services') or stripped.lower().startswith('the data'):
                break
            data_lines.append(line)

        reader = csv.DictReader(io.StringIO('\n'.join(data_lines)))

        imported = 0
        skipped = 0
        errors = []

        conn = self._get_conn()
        cursor = conn.cursor()

        for row in reader:
            try:
                date_str = row.get('Run Date', '').strip().strip('"')
                action = row.get('Action', '').strip().strip('"')
                symbol = row.get('Symbol', '').strip().strip('"')
                description = row.get('Security Description', '').strip().strip('"')
                amount_str = row.get('Amount ($)', '').strip().strip('"').replace(',', '')

                if not date_str or not amount_str or amount_str in ('', '--'):
                    skipped += 1
                    continue

                # Parse date (M/DD/YYYY or MM/DD/YYYY)
                try:
                    date = datetime.strptime(date_str, '%m/%d/%Y').date().isoformat()
                except ValueError:
                    skipped += 1
                    continue

                amount = float(amount_str)

                # Build human-readable description
                txn_desc = action
                if symbol and symbol != '--':
                    txn_desc += f": {symbol}"
                if description and description != '--':
                    txn_desc += f" — {description}"

                # Category based on action type
                category = self._categorise_fidelity_action(action)

                # Stable unique ID
                txn_id = self._make_id('fidelity', date_str, action, symbol, amount_str)

                cursor.execute("""
                    INSERT OR IGNORE INTO transactions
                        (transaction_id, account_id, date, amount, description,
                         category, source_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'csv', ?)
                """, (txn_id, account_id, date, amount, txn_desc, category,
                      datetime.now().isoformat()))

                if cursor.rowcount:
                    imported += 1
                else:
                    skipped += 1

            except Exception as e:
                errors.append(str(e))

        conn.commit()
        conn.close()

        return {
            "success": True,
            "source": institution,
            "rows_imported": imported,
            "skipped": skipped,
            "errors": errors or None
        }

    def _categorise_fidelity_action(self, action: str) -> str:
        action_lower = action.lower()
        if any(w in action_lower for w in ['dividend', 'interest', 'reinvestment']):
            return 'Dividend & Interest'
        if any(w in action_lower for w in ['you bought', 'purchase', 'buy']):
            return 'Buy'
        if any(w in action_lower for w in ['you sold', 'sale', 'sell']):
            return 'Sell'
        if any(w in action_lower for w in ['transferred', 'transfer', 'journaled']):
            return 'Transfer'
        if any(w in action_lower for w in ['fee', 'charge', 'advisory']):
            return 'Fees'
        if any(w in action_lower for w in ['tax', 'withholding']):
            return 'Tax'
        if any(w in action_lower for w in ['contribution', 'deposit', 'rollover']):
            return 'Contribution'
        if any(w in action_lower for w in ['distribution', 'withdrawal']):
            return 'Distribution'
        return 'Investment Activity'

    # =========================================================================
    # Fidelity positions (holdings)
    # =========================================================================

    def _import_fidelity_positions(self, content: str) -> Dict[str, Any]:
        """
        Parse Fidelity's positions CSV export.

        Actual format (as exported Mar 2026):
          Row 1: Header — Account Number, Account Name, Symbol, Description,
                          Quantity, Last Price, Last Price Change, Current Value,
                          Today's Gain/Loss Dollar, Today's Gain/Loss Percent,
                          Total Gain/Loss Dollar, Total Gain/Loss Percent,
                          Percent Of Account, Cost Basis Total, Average Cost Basis, Type
          Rows 2+: One holding per row; multiple accounts interleaved by Account Number
          Footer: blank line then disclaimer text
        """
        # Strip footer — stop at first blank line after data starts
        clean_lines = []
        data_started = False
        for line in content.splitlines():
            if not data_started:
                clean_lines.append(line)
                data_started = True
                continue
            # Stop at blank lines or disclaimer text
            stripped = line.strip().strip('"')
            if stripped == '' or stripped.lower().startswith('the data') or stripped.lower().startswith('brokerage'):
                break
            clean_lines.append(line)

        reader = csv.DictReader(io.StringIO('\n'.join(clean_lines)))

        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        # Group by account number so we upsert each account once
        accounts_seen = {}
        imported = 0
        skipped = 0
        errors = []

        def clean_num(val: str) -> Optional[float]:
            """Strip $, +, commas, percent signs and convert to float."""
            v = val.strip().strip('"').replace('$', '').replace('+', '').replace(',', '').replace('%', '')
            if v in ('', '--', 'N/A'):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        for row in reader:
            try:
                account_number = row.get('Account Number', '').strip()
                account_name   = row.get('Account Name', '').strip()
                symbol         = row.get('Symbol', '').strip()
                description    = row.get('Description', '').strip()
                asset_type     = row.get('Type', '').strip() or 'Stock'

                # Skip footer/total rows
                if not symbol or symbol.lower() in ('total', '--', ''):
                    skipped += 1
                    continue

                quantity      = clean_num(row.get('Quantity', ''))
                price         = clean_num(row.get('Last Price', ''))
                current_value = clean_num(row.get('Current Value', ''))
                cost_basis    = clean_num(row.get('Cost Basis Total', ''))

                account_id = f"fidelity_{account_number}"
                institution = f"Fidelity — {account_name}" if account_name else "Fidelity"

                # Upsert investment account once per account number
                if account_number not in accounts_seen:
                    accounts_seen[account_number] = account_name
                    cursor.execute("""
                        INSERT INTO investment_accounts
                            (account_id, account_name, institution, account_type,
                             is_active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(account_id) DO UPDATE SET
                            account_name=excluded.account_name,
                            institution=excluded.institution,
                            account_type=excluded.account_type,
                            is_active=1, updated_at=excluded.updated_at
                    """, (account_id, account_name or "Fidelity Account",
                          institution, asset_type or 'Brokerage', now, now))

                    # Mark existing holdings for this account inactive before re-importing
                    cursor.execute(
                        "UPDATE holdings SET is_active = 0 WHERE account_id = ?",
                        (account_id,)
                    )

                cursor.execute("""
                    INSERT INTO holdings
                        (account_id, symbol, name, quantity, price, current_value,
                         cost_basis, asset_type, is_active, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (account_id, symbol, description, quantity, price,
                      current_value, cost_basis, asset_type, now))
                imported += 1

            except Exception as e:
                errors.append(str(e))

        conn.commit()
        conn.close()

        accounts_count = len(accounts_seen)
        return {
            "success": True,
            "source": "Fidelity",
            "rows_imported": imported,
            "skipped": skipped,
            "type": "positions",
            "accounts_updated": accounts_count,
            "errors": errors or None
        }

    # =========================================================================
    # Generic CSV fallback
    # =========================================================================

    def _import_generic_transactions(self, content: str, filename: str) -> Dict[str, Any]:
        """
        Import a generic transaction CSV. Attempts to map common column names
        to the transactions table.

        Recognised column names (case-insensitive):
          date, amount, description/memo/name/payee, category, account
        """
        reader = csv.DictReader(io.StringIO(content))
        headers = {h.lower().strip(): h for h in (reader.fieldnames or [])}

        date_col = next((headers[h] for h in headers if 'date' in h), None)
        amount_col = next((headers[h] for h in headers if 'amount' in h), None)
        desc_col = next((headers[h] for h in ['description', 'memo', 'name', 'payee', 'narrative']
                         if h in headers), None)
        cat_col = next((headers[h] for h in headers if 'categ' in h), None)

        if not date_col or not amount_col:
            return {
                "success": False,
                "error": f"Could not find required Date and Amount columns. Found: {list(headers.keys())}"
            }

        account_id = f"csv_{re.sub(r'[^a-z0-9]', '_', filename.lower().rsplit('.', 1)[0])}"
        self._upsert_account(account_id, filename.rsplit('.', 1)[0], 'unknown', 'CSV Import')

        conn = self._get_conn()
        cursor = conn.cursor()
        imported = 0
        skipped = 0
        errors = []

        for row in reader:
            try:
                date_str = row.get(date_col, '').strip()
                amount_str = row.get(amount_col, '').strip().replace(',', '').replace('$', '')
                description = row.get(desc_col, '').strip() if desc_col else ''
                category = row.get(cat_col, '').strip() if cat_col else None

                if not date_str or not amount_str:
                    skipped += 1
                    continue

                # Try several date formats
                date = None
                for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%d/%m/%Y', '%m-%d-%Y', '%b %d, %Y'):
                    try:
                        date = datetime.strptime(date_str, fmt).date().isoformat()
                        break
                    except ValueError:
                        continue

                if not date:
                    skipped += 1
                    errors.append(f"Unrecognised date format: {date_str}")
                    continue

                amount = float(amount_str)
                txn_id = self._make_id('csv', date_str, description, amount_str)

                cursor.execute("""
                    INSERT OR IGNORE INTO transactions
                        (transaction_id, account_id, date, amount, description,
                         category, source_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'csv', ?)
                """, (txn_id, account_id, date, amount, description, category,
                      datetime.now().isoformat()))

                if cursor.rowcount:
                    imported += 1
                else:
                    skipped += 1

            except Exception as e:
                errors.append(str(e))

        conn.commit()
        conn.close()

        return {
            "success": True,
            "source": filename,
            "rows_imported": imported,
            "skipped": skipped,
            "errors": errors or None
        }

    # =========================================================================
    # Helpers
    # =========================================================================

    def _upsert_account(self, account_id: str, name: str, acct_type: str, institution: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO accounts (account_id, name, type, institution, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                name=excluded.name, institution=excluded.institution,
                is_active=1, updated_at=excluded.updated_at
        """, (account_id, name, acct_type, institution, now, now))
        conn.commit()
        conn.close()

    def _make_id(self, *parts) -> str:
        raw = '|'.join(str(p) for p in parts)
        return 'csv_' + hashlib.sha1(raw.encode()).hexdigest()[:20]

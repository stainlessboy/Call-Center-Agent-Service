"""Telegram Mini App backend — REST/WS layer over the existing domain logic.

The Mini App needs *data* where the Telegram bot needs *text*. Nothing in this
package re-implements business rules: product listing, qualification trees,
rate selection, amortization and office search all come from the same modules
the LangGraph nodes call (``app.agent.products``, ``app.agent.qualify``,
``app.agent.rate_rules``, ``app.agent.branches``, ``app.utils.*``).
"""

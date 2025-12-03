"""
Alert system for AbstractFinance.
Provides Telegram and email notifications for trading events.
"""

import os
import asyncio
from datetime import datetime, date
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

from .logging_utils import get_trading_logger


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertType(Enum):
    """Types of alerts."""
    DAILY_SUMMARY = "daily_summary"
    POSITION_CHANGE = "position_change"
    RISK_WARNING = "risk_warning"
    CRISIS_ALERT = "crisis_alert"
    CONNECTION_ERROR = "connection_error"
    ORDER_REJECTION = "order_rejection"
    HEDGE_BUDGET = "hedge_budget"
    DRAWDOWN_WARNING = "drawdown_warning"
    PNL_ALERT = "pnl_alert"


@dataclass
class Alert:
    """Alert message structure."""
    alert_type: AlertType
    severity: AlertSeverity
    title: str
    message: str
    timestamp: datetime
    metadata: Optional[Dict[str, Any]] = None

    def format_telegram(self) -> str:
        """Format alert for Telegram."""
        severity_emoji = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.ERROR: "🚨",
            AlertSeverity.CRITICAL: "🔴"
        }

        lines = [
            f"{severity_emoji.get(self.severity, '•')} *{self.title}*",
            "",
            self.message,
            "",
            f"_Time: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}_"
        ]

        return "\n".join(lines)

    def format_email_html(self) -> str:
        """Format alert for email HTML."""
        severity_color = {
            AlertSeverity.INFO: "#17a2b8",
            AlertSeverity.WARNING: "#ffc107",
            AlertSeverity.ERROR: "#dc3545",
            AlertSeverity.CRITICAL: "#721c24"
        }

        return f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <div style="background-color: {severity_color.get(self.severity, '#6c757d')};
                        color: white; padding: 10px; border-radius: 5px;">
                <h2>{self.title}</h2>
            </div>
            <div style="padding: 15px;">
                <p>{self.message.replace(chr(10), '<br>')}</p>
                <hr>
                <p style="color: #6c757d; font-size: 12px;">
                    Alert Type: {self.alert_type.value}<br>
                    Severity: {self.severity.value}<br>
                    Time: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}
                </p>
            </div>
        </body>
        </html>
        """


class TelegramNotifier:
    """Sends alerts via Telegram."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        enabled: bool = True
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token
            chat_id: Chat ID to send messages to
            enabled: Whether notifications are enabled
        """
        if not TELEGRAM_AVAILABLE:
            raise ImportError("python-telegram-bot is required for Telegram notifications")

        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.bot = Bot(token=bot_token) if enabled else None
        self.logger = get_trading_logger()

    async def send_async(self, alert: Alert) -> bool:
        """
        Send alert asynchronously.

        Args:
            alert: Alert to send

        Returns:
            True if sent successfully
        """
        if not self.enabled or not self.bot:
            return False

        try:
            message = alert.format_telegram()
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='Markdown'
            )
            return True
        except TelegramError as e:
            self.logger.log_alert(
                alert_type="telegram_error",
                severity="warning",
                message=f"Failed to send Telegram alert: {e}"
            )
            return False

    def send(self, alert: Alert) -> bool:
        """
        Send alert synchronously.

        Args:
            alert: Alert to send

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.send_async(alert))


class EmailNotifier:
    """Sends alerts via email."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        recipient: str,
        sender: Optional[str] = None,
        enabled: bool = True
    ):
        """
        Initialize email notifier.

        Args:
            smtp_host: SMTP server host
            smtp_port: SMTP server port
            username: SMTP username
            password: SMTP password
            recipient: Email recipient
            sender: Email sender (defaults to username)
            enabled: Whether notifications are enabled
        """
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.recipient = recipient
        self.sender = sender or username
        self.enabled = enabled
        self.logger = get_trading_logger()

    def send(self, alert: Alert) -> bool:
        """
        Send alert via email.

        Args:
            alert: Alert to send

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"[AbstractFinance] {alert.severity.value.upper()}: {alert.title}"
            msg['From'] = self.sender
            msg['To'] = self.recipient

            # Plain text version
            text_part = MIMEText(alert.message, 'plain')
            msg.attach(text_part)

            # HTML version
            html_part = MIMEText(alert.format_email_html(), 'html')
            msg.attach(html_part)

            # Send
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)

            return True

        except Exception as e:
            self.logger.log_alert(
                alert_type="email_error",
                severity="warning",
                message=f"Failed to send email alert: {e}"
            )
            return False


class AlertManager:
    """
    Manages alert routing and delivery.
    Central point for all system alerts.
    """

    def __init__(self, settings: Dict[str, Any]):
        """
        Initialize alert manager.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.logger = get_trading_logger()

        alert_settings = settings.get('alerts', {})
        self.enabled = alert_settings.get('enabled', True)

        # Alert thresholds
        thresholds = alert_settings.get('thresholds', {})
        self.daily_loss_threshold = thresholds.get('daily_loss_pct', -0.03)
        self.daily_gain_threshold = thresholds.get('daily_gain_pct', 0.05)
        self.hedge_budget_threshold = thresholds.get('hedge_budget_usage_pct', 0.90)

        # Initialize notifiers
        self.telegram: Optional[TelegramNotifier] = None
        self.email: Optional[EmailNotifier] = None

        self._init_telegram(alert_settings)
        self._init_email(alert_settings)

    def _init_telegram(self, settings: Dict[str, Any]) -> None:
        """Initialize Telegram notifier."""
        telegram_settings = settings.get('telegram', {})
        if telegram_settings.get('enabled', False):
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', telegram_settings.get('bot_token'))
            chat_id = os.environ.get('TELEGRAM_CHAT_ID', telegram_settings.get('chat_id'))

            if bot_token and chat_id and TELEGRAM_AVAILABLE:
                try:
                    self.telegram = TelegramNotifier(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        enabled=True
                    )
                except Exception as e:
                    self.logger.log_alert(
                        alert_type="telegram_init_error",
                        severity="warning",
                        message=f"Failed to initialize Telegram: {e}"
                    )

    def _init_email(self, settings: Dict[str, Any]) -> None:
        """Initialize email notifier."""
        email_settings = settings.get('email', {})
        if email_settings.get('enabled', False):
            try:
                self.email = EmailNotifier(
                    smtp_host=os.environ.get('EMAIL_SMTP_HOST', email_settings.get('smtp_host', '')),
                    smtp_port=int(os.environ.get('EMAIL_SMTP_PORT', email_settings.get('smtp_port', 587))),
                    username=os.environ.get('EMAIL_USERNAME', email_settings.get('username', '')),
                    password=os.environ.get('EMAIL_PASSWORD', email_settings.get('password', '')),
                    recipient=os.environ.get('EMAIL_RECIPIENT', email_settings.get('recipient', '')),
                    enabled=True
                )
            except Exception as e:
                self.logger.log_alert(
                    alert_type="email_init_error",
                    severity="warning",
                    message=f"Failed to initialize email: {e}"
                )

    def send_alert(self, alert: Alert) -> Dict[str, bool]:
        """
        Send alert through all configured channels.

        Args:
            alert: Alert to send

        Returns:
            Dict mapping channel to success status
        """
        if not self.enabled:
            return {}

        results = {}

        # Log the alert
        self.logger.log_alert(
            alert_type=alert.alert_type.value,
            severity=alert.severity.value,
            message=alert.message,
            metadata=alert.metadata
        )

        # Send via Telegram
        if self.telegram:
            results['telegram'] = self.telegram.send(alert)

        # Send via email for warnings and above
        if self.email and alert.severity in [AlertSeverity.WARNING, AlertSeverity.ERROR, AlertSeverity.CRITICAL]:
            results['email'] = self.email.send(alert)

        return results

    def send_daily_summary(
        self,
        nav: float,
        daily_pnl: float,
        daily_return: float,
        gross_exposure: float,
        net_exposure: float,
        realized_vol: float,
        drawdown: float,
        hedge_budget: float
    ) -> None:
        """Send daily summary alert."""
        message_lines = [
            f"NAV: ${nav:,.2f}",
            f"Daily P&L: ${daily_pnl:,.2f} ({daily_return:.2%})",
            "",
            f"Gross Exposure: ${gross_exposure:,.0f}",
            f"Net Exposure: ${net_exposure:,.0f}",
            "",
            f"Realized Vol (20d): {realized_vol:.1%}",
            f"Current Drawdown: {drawdown:.2%}",
            f"Hedge Budget Used YTD: ${hedge_budget:,.0f}"
        ]

        alert = Alert(
            alert_type=AlertType.DAILY_SUMMARY,
            severity=AlertSeverity.INFO,
            title=f"Daily Summary - {date.today().isoformat()}",
            message="\n".join(message_lines),
            timestamp=datetime.utcnow(),
            metadata={
                "nav": nav,
                "daily_return": daily_return
            }
        )

        self.send_alert(alert)

        # Check for additional alerts
        self._check_pnl_alert(daily_return)
        self._check_drawdown_alert(drawdown)
        self._check_hedge_budget_alert(hedge_budget, nav)

    def _check_pnl_alert(self, daily_return: float) -> None:
        """Check if P&L triggers an alert."""
        if daily_return <= self.daily_loss_threshold:
            alert = Alert(
                alert_type=AlertType.PNL_ALERT,
                severity=AlertSeverity.WARNING,
                title="Large Daily Loss",
                message=f"Daily return of {daily_return:.2%} exceeds loss threshold of {self.daily_loss_threshold:.2%}",
                timestamp=datetime.utcnow()
            )
            self.send_alert(alert)

        elif daily_return >= self.daily_gain_threshold:
            alert = Alert(
                alert_type=AlertType.PNL_ALERT,
                severity=AlertSeverity.INFO,
                title="Large Daily Gain",
                message=f"Daily return of {daily_return:.2%} exceeds gain threshold of {self.daily_gain_threshold:.2%}",
                timestamp=datetime.utcnow()
            )
            self.send_alert(alert)

    def _check_drawdown_alert(self, drawdown: float) -> None:
        """Check if drawdown triggers an alert."""
        if drawdown <= -0.05:  # 5% drawdown warning
            severity = AlertSeverity.WARNING if drawdown > -0.10 else AlertSeverity.ERROR
            alert = Alert(
                alert_type=AlertType.DRAWDOWN_WARNING,
                severity=severity,
                title="Drawdown Warning",
                message=f"Current drawdown of {drawdown:.2%} is significant",
                timestamp=datetime.utcnow()
            )
            self.send_alert(alert)

    def _check_hedge_budget_alert(self, hedge_used: float, nav: float) -> None:
        """Check if hedge budget usage triggers an alert."""
        budget_annual = nav * self.settings.get('hedge_budget_annual_pct', 0.025)
        if budget_annual > 0:
            usage_pct = hedge_used / budget_annual
            if usage_pct >= self.hedge_budget_threshold:
                alert = Alert(
                    alert_type=AlertType.HEDGE_BUDGET,
                    severity=AlertSeverity.WARNING,
                    title="Hedge Budget Warning",
                    message=f"Hedge budget usage at {usage_pct:.1%} of annual allocation",
                    timestamp=datetime.utcnow()
                )
                self.send_alert(alert)

    def send_crisis_alert(
        self,
        vix_level: float,
        action_taken: str,
        details: str
    ) -> None:
        """Send crisis alert."""
        alert = Alert(
            alert_type=AlertType.CRISIS_ALERT,
            severity=AlertSeverity.CRITICAL,
            title="CRISIS ALERT",
            message=f"VIX Level: {vix_level:.1f}\nAction: {action_taken}\n\n{details}",
            timestamp=datetime.utcnow()
        )
        self.send_alert(alert)

    def send_connection_error(self, error_message: str) -> None:
        """Send connection error alert."""
        alert = Alert(
            alert_type=AlertType.CONNECTION_ERROR,
            severity=AlertSeverity.ERROR,
            title="Connection Error",
            message=error_message,
            timestamp=datetime.utcnow()
        )
        self.send_alert(alert)

    def send_order_rejection(
        self,
        instrument: str,
        side: str,
        quantity: float,
        reason: str
    ) -> None:
        """Send order rejection alert."""
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            title="Order Rejected",
            message=f"Order {side} {quantity} {instrument} was rejected.\nReason: {reason}",
            timestamp=datetime.utcnow()
        )
        self.send_alert(alert)

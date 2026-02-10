"""
M365 Email Fetcher for Newsletter Curator.

Connects to an Outlook mailbox via Microsoft Graph API to fetch
newsletters from the "to qualify" folder and move processed ones
to "to qualify/processed".
"""

import os

from azure.identity import ClientSecretCredential
from dotenv import load_dotenv
from kiota_abstractions.base_request_configuration import RequestConfiguration
from msgraph import GraphServiceClient
from msgraph.generated.users.item.mail_folders.item.messages.item.move.move_post_request_body import (
    MovePostRequestBody,
)

load_dotenv()


class EmailFetcher:
    """
    Fetches newsletter emails from Outlook via Microsoft Graph.

    Usage:
        fetcher = EmailFetcher()  # uses env vars from .env
        emails = await fetcher.fetch_emails()
        for email in emails:
            print(email["subject"])
        await fetcher.move_to_processed(emails[0]["id"])
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str | None = None,
        user_email: str | None = None,
    ):
        """
        Initialize the email fetcher.

        Args:
            client_id: Azure app client ID. Falls back to MS_GRAPH_CLIENT_ID env var.
            client_secret: Azure app client secret. Falls back to MS_GRAPH_CLIENT_SECRET env var.
            tenant_id: Azure tenant ID. Falls back to MS_GRAPH_TENANT_ID env var.
            user_email: Mailbox to read from. Falls back to MS_GRAPH_USER_EMAIL env var.
        """
        self._client_id = client_id or os.environ.get("MS_GRAPH_CLIENT_ID")
        self._client_secret = client_secret or os.environ.get("MS_GRAPH_CLIENT_SECRET")
        self._tenant_id = tenant_id or os.environ.get("MS_GRAPH_TENANT_ID")
        self._user_email = user_email or os.environ.get("MS_GRAPH_USER_EMAIL")

        missing = []
        if not self._client_id:
            missing.append("MS_GRAPH_CLIENT_ID")
        if not self._client_secret:
            missing.append("MS_GRAPH_CLIENT_SECRET")
        if not self._tenant_id:
            missing.append("MS_GRAPH_TENANT_ID")
        if not self._user_email:
            missing.append("MS_GRAPH_USER_EMAIL")

        if missing:
            raise ValueError(
                f"Missing credentials: {', '.join(missing)}. "
                "Set them in .env or pass to EmailFetcher()."
            )

        credential = ClientSecretCredential(
            tenant_id=self._tenant_id,
            client_id=self._client_id,
            client_secret=self._client_secret,
        )
        self._client = GraphServiceClient(
            credentials=credential,
            scopes=["https://graph.microsoft.com/.default"],
        )

        # Cached folder IDs — populated by _find_folders()
        self._qualify_folder_id: str | None = None
        self._processed_folder_id: str | None = None

    # ── Folder discovery ──────────────────────────────────────────

    async def _find_folders(self) -> None:
        """
        Find the "to qualify" mail folder and its "processed" subfolder.

        Sets self._qualify_folder_id and self._processed_folder_id.
        Raises RuntimeError if either folder is not found.
        """
        if self._qualify_folder_id and self._processed_folder_id:
            return

        user = self._client.users.by_user_id(self._user_email)

        # "To qualify" lives under Inbox — find Inbox first
        folders_req = user.mail_folders
        query_params = folders_req.MailFoldersRequestBuilderGetQueryParameters(
            select=["id", "displayName", "childFolderCount"],
            top=100,
        )
        config = RequestConfiguration(query_parameters=query_params)
        result = await folders_req.get(config)

        inbox_id = None
        for folder in result.value or []:
            if folder.display_name and folder.display_name.lower() == "inbox":
                inbox_id = folder.id
                break

        if not inbox_id:
            raise RuntimeError("Inbox folder not found.")

        # Find "To qualify" among Inbox subfolders
        inbox_children_req = user.mail_folders.by_mail_folder_id(
            inbox_id
        ).child_folders
        inbox_children_params = (
            inbox_children_req.ChildFoldersRequestBuilderGetQueryParameters(
                select=["id", "displayName", "childFolderCount"],
                top=100,
            )
        )
        inbox_children_config = RequestConfiguration(
            query_parameters=inbox_children_params
        )
        inbox_children_result = await inbox_children_req.get(inbox_children_config)

        for folder in inbox_children_result.value or []:
            if folder.display_name and folder.display_name.lower() == "to qualify":
                self._qualify_folder_id = folder.id
                break

        if not self._qualify_folder_id:
            raise RuntimeError(
                "Mail folder 'To qualify' not found under Inbox. "
                "Please create it in Outlook."
            )

        # Find "processed" subfolder inside "To qualify"
        child_req = user.mail_folders.by_mail_folder_id(
            self._qualify_folder_id
        ).child_folders
        child_params = child_req.ChildFoldersRequestBuilderGetQueryParameters(
            select=["id", "displayName"],
            top=100,
        )
        child_config = RequestConfiguration(query_parameters=child_params)
        child_result = await child_req.get(child_config)

        for folder in child_result.value or []:
            if folder.display_name and folder.display_name.lower() == "processed":
                self._processed_folder_id = folder.id
                break

        if not self._processed_folder_id:
            raise RuntimeError(
                "Subfolder 'processed' not found inside 'To qualify'. "
                "Please create it in Outlook."
            )

    # ── Fetching emails ───────────────────────────────────────────

    async def fetch_emails(self) -> list[dict]:
        """
        Fetch all emails from the "to qualify" folder.

        Returns:
            List of dicts with keys: id, subject, sender, received_at, body_html
        """
        await self._find_folders()

        user = self._client.users.by_user_id(self._user_email)
        messages_req = user.mail_folders.by_mail_folder_id(
            self._qualify_folder_id
        ).messages

        all_messages = []
        next_link = None

        while True:
            if next_link:
                messages_req = messages_req.with_url(next_link)
                result = await messages_req.get()
            else:
                query_params = messages_req.MessagesRequestBuilderGetQueryParameters(
                    select=["id", "subject", "from", "receivedDateTime", "body"],
                    top=100,
                )
                config = RequestConfiguration(query_parameters=query_params)
                result = await messages_req.get(config)

            for msg in result.value or []:
                all_messages.append(self._extract_message(msg))

            if not result.odata_next_link:
                break
            next_link = result.odata_next_link

        return all_messages

    # ── Moving emails ─────────────────────────────────────────────

    async def move_to_processed(self, message_id: str) -> None:
        """
        Move an email from "to qualify" to "to qualify/processed".

        Args:
            message_id: The Graph message ID to move.
        """
        await self._find_folders()

        user = self._client.users.by_user_id(self._user_email)
        message_req = user.mail_folders.by_mail_folder_id(
            self._qualify_folder_id
        ).messages.by_message_id(message_id)

        move_body = MovePostRequestBody()
        move_body.destination_id = self._processed_folder_id

        await message_req.move.post(move_body)

    # ── Single email body ─────────────────────────────────────────

    async def get_email_body(self, message_id: str) -> str:
        """
        Get the HTML body for a single email.

        Args:
            message_id: The Graph message ID.

        Returns:
            The HTML body content as a string.
        """
        await self._find_folders()

        user = self._client.users.by_user_id(self._user_email)
        message_req = user.mail_folders.by_mail_folder_id(
            self._qualify_folder_id
        ).messages.by_message_id(message_id)

        query_params = message_req.MessageItemRequestBuilderGetQueryParameters(
            select=["body"],
        )
        config = RequestConfiguration(query_parameters=query_params)
        msg = await message_req.get(config)

        if msg and msg.body and msg.body.content:
            return msg.body.content
        return ""

    # ── Inbox search ─────────────────────────────────────────────

    async def search_inbox(
        self,
        sender_contains: str | None = None,
        received_after: str | None = None,
        top: int = 10,
    ) -> list[dict]:
        """
        Search recent messages in the Inbox folder.

        Used by BrowserSession to find Medium magic link emails.

        Args:
            sender_contains: Filter to emails where sender address contains this string.
            received_after: ISO datetime string; only return emails received after this time.
            top: Maximum number of messages to return.

        Returns:
            List of dicts with keys: id, subject, sender, received_at, body_html
        """
        user = self._client.users.by_user_id(self._user_email)

        # Find Inbox folder ID
        folders_req = user.mail_folders
        query_params = folders_req.MailFoldersRequestBuilderGetQueryParameters(
            select=["id", "displayName"],
            top=100,
        )
        config = RequestConfiguration(query_parameters=query_params)
        result = await folders_req.get(config)

        inbox_id = None
        for folder in result.value or []:
            if folder.display_name and folder.display_name.lower() == "inbox":
                inbox_id = folder.id
                break

        if not inbox_id:
            raise RuntimeError("Inbox folder not found.")

        # Build OData filter for receivedDateTime
        odata_filter = None
        if received_after:
            odata_filter = f"receivedDateTime ge {received_after}"

        # Fetch messages
        messages_req = user.mail_folders.by_mail_folder_id(inbox_id).messages
        msg_params = messages_req.MessagesRequestBuilderGetQueryParameters(
            select=["id", "subject", "from", "receivedDateTime", "body"],
            top=top,
            orderby=["receivedDateTime DESC"],
            filter=odata_filter,
        )
        msg_config = RequestConfiguration(query_parameters=msg_params)
        msg_result = await messages_req.get(msg_config)

        messages = []
        for msg in msg_result.value or []:
            extracted = self._extract_message(msg)
            # Filter by sender in Python (more reliable than OData)
            if sender_contains:
                if sender_contains.lower() not in extracted["sender"].lower():
                    continue
            messages.append(extracted)

        return messages

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(msg) -> dict:
        """Convert a Graph Message object to a clean dict."""
        sender = ""
        sender_name = ""
        if msg.from_ and msg.from_.email_address:
            sender = msg.from_.email_address.address or ""
            sender_name = msg.from_.email_address.name or ""

        body_html = ""
        if msg.body and msg.body.content:
            body_html = msg.body.content

        received_at = ""
        if msg.received_date_time:
            received_at = msg.received_date_time.isoformat()

        return {
            "id": msg.id,
            "subject": msg.subject or "",
            "sender": sender,
            "sender_name": sender_name,
            "received_at": received_at,
            "body_html": body_html,
        }

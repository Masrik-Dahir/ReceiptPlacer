import os
import re
import json
import boto3
import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def lambda_handler(event, context):
    """
    For each item in parent_folder_id:
      - If it's a folder (mimeType = 'application/vnd.google-apps.folder'), skip it.
      - If filename matches our date regex, parse date from name.
      - Otherwise, use file's createdTime to determine year/month.
      - Organize the file into a year => month folder structure.
    """

    # -----------------------------------------------------------
    # 0. Configuration: folder, secrets, region, etc.
    # -----------------------------------------------------------
    parent_folder_id = event.get('parent_folder_id') or os.getenv('PARENT_FOLDER_ID')
    if not parent_folder_id:
        raise ValueError("No parent_folder_id provided via event or environment.")

    region_name = os.getenv('REGION_NAME', 'us-east-1')
    secret_name = os.getenv('SECRET_NAME', 'my-google-service-account')

    # -----------------------------------------------------------
    # 1. Retrieve Service Account credentials from AWS Secrets Manager
    # -----------------------------------------------------------
    try:
        secrets_client = boto3.client('secretsmanager', region_name=region_name)
        response = secrets_client.get_secret_value(SecretId=secret_name)
        secret_json = json.loads(response['SecretString'])

        # Suppose you store all the standard fields at top level:
        service_account_info = {
            "type": secret_json["type"],
            "project_id": secret_json["project_id"],
            "private_key_id": secret_json["private_key_id"],
            "private_key": secret_json["private_key"],
            "client_email": secret_json["client_email"],
            "client_id": secret_json["client_id"],
            "auth_uri": secret_json["auth_uri"],
            "token_uri": secret_json["token_uri"],
            "auth_provider_x509_cert_url": secret_json["auth_provider_x509_cert_url"],
            "client_x509_cert_url": secret_json["client_x509_cert_url"],
        }

        credentials = Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        drive_service = build('drive', 'v3', credentials=credentials)
    except Exception as e:
        raise RuntimeError(f"Error retrieving credentials from Secrets Manager: {e}")

    # -----------------------------------------------------------
    # 2. Regex to capture common date formats from filename
    # -----------------------------------------------------------
    pattern = re.compile(
        r'^'
        r'.*('
        # 1) "Feb 27, 2025"
        r'(?:[A-Za-z]{3}\s+\d{1,2},\s+\d{4})'
        r'|'
        # 2) "02/27/2025"
        r'(?:\d{1,2}/\d{1,2}/\d{4})'
        r'|'
        # 3) "2025-02-27"
        r'(?:\d{4}-\d{1,2}-\d{1,2})'
        r')'
        r'.*\.pdf$',
        re.IGNORECASE
    )

    # -----------------------------------------------------------
    # 3. Helper function: find or create a folder
    # -----------------------------------------------------------
    def find_or_create_folder(folder_name, parent_id):
        try:
            query = (
                f"mimeType = 'application/vnd.google-apps.folder' "
                f"and name = '{folder_name}' "
                f"and '{parent_id}' in parents "
                f"and trashed = false"
            )
            response = drive_service.files().list(
                q=query,
                spaces='drive',
                fields="files(id, name)"
            ).execute()
            folders = response.get('files', [])

            if folders:
                return folders[0]['id']
            else:
                file_metadata = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id]
                }
                new_folder = drive_service.files().create(
                    body=file_metadata, fields='id'
                ).execute()
                return new_folder.get('id')
        except HttpError as error:
            raise RuntimeError(f"Error while finding/creating folder {folder_name}: {error}")

    # -----------------------------------------------------------
    # 4. Build year/month structure
    # -----------------------------------------------------------
    MONTH_MAP = {
        1: 'January', 2: 'February', 3: 'March',
        4: 'April', 5: 'May', 6: 'June',
        7: 'July', 8: 'August', 9: 'September',
        10: 'October', 11: 'November', 12: 'December'
    }

    def ensure_year_and_month_folders(year):
        """Create a year folder under parent_folder_id, plus all months, if not exist."""
        year_folder_id = find_or_create_folder(str(year), parent_folder_id)
        for m in range(1, 13):
            find_or_create_folder(MONTH_MAP[m], year_folder_id)
        return year_folder_id

    # -----------------------------------------------------------
    # 5. Parse date string from the regex match
    # -----------------------------------------------------------
    from datetime import datetime

    def parse_date_string(date_str):
        """
        Attempt to parse a string that might look like:
          - "Feb 27, 2025"
          - "02/27/2025"
          - "2025-02-27"
        Return a (year, month) tuple or raise ValueError if unrecognized.
        """
        # Try different formats in sequence:
        formats = [
            "%b %d, %Y",  # "Feb 27, 2025"
            "%m/%d/%Y",  # "02/27/2025"
            "%Y-%m-%d",  # "2025-02-27"
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.year, dt.month
            except ValueError:
                pass

        raise ValueError(f"Could not parse date string: {date_str}")

    # -----------------------------------------------------------
    # 6. List files and folders in the parent folder
    #    We filter out the folders below
    # -----------------------------------------------------------
    try:
        query = (
            f"'{parent_folder_id}' in parents "
            f"and trashed = false"
        )
        response = drive_service.files().list(
            q=query,
            spaces='drive',
            fields="files(id, name, mimeType, createdTime)"
        ).execute()
        items = response.get('files', [])
    except HttpError as error:
        raise RuntimeError(f"Error while listing items in parent folder: {error}")

    # -----------------------------------------------------------
    # 7. For each item: skip folders, handle files
    # -----------------------------------------------------------
    processed_count = 0
    for info in items:
        file_id = info['id']
        file_name = info['name']
        mime_type = info.get('mimeType')

        # Skip if this item is a folder
        if mime_type == 'application/vnd.google-apps.folder':
            print(f"Skipping folder '{file_name}' (id={file_id}).")
            continue

        # Attempt to match the filename
        m = pattern.match(file_name)

        try:
            if m:
                # Extract the entire date substring from the match
                date_substring = m.group(1)
                year, month = parse_date_string(date_substring)
            else:
                # Fallback: use the file's createdTime
                # (e.g. "2025-02-27T15:00:03.000Z")
                created_str = info.get('createdTime')
                dt = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                year = dt.year
                month = dt.month

            # Now ensure year / month folders exist
            year_folder_id = ensure_year_and_month_folders(year)
            month_folder_id = find_or_create_folder(MONTH_MAP[month], year_folder_id)

            # Move the file
            current_parents = drive_service.files().get(
                fileId=file_id,
                fields='parents'
            ).execute().get('parents', [])

            drive_service.files().update(
                fileId=file_id,
                addParents=month_folder_id,
                removeParents=",".join(current_parents),
                fields='id, parents'
            ).execute()

            processed_count += 1
        except Exception as e:
            print(f"Skipping file '{file_name}' (id={file_id}) due to error: {e}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": f"Processed {processed_count} files in folder {parent_folder_id}.",
            "totalItemsFound": len(items)
        })
    }

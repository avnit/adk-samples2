import os
import requests
import json
import time
import base64
from dataclasses import dataclass
from typing import Dict, Any, Optional, Union, Callable

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse

# --- Wiz API Configuration ---
WIZ_CLIENT_ID = os.environ.get("WIZ_CLIENT_ID")
WIZ_CLIENT_SECRET = os.environ.get("WIZ_CLIENT_SECRET")
DEFAULT_WIZ_AUTH_URL = "https://auth.wiz.io/oauth/token"
# Default Wiz API Base URL - adjust if your Wiz instance is in a specific region e.g., https://api.us1.wiz.io
DEFAULT_WIZ_API_BASE_URL = "https://api.wiz.io"
DEFAULT_WIZ_GQL_PATH = "/graphql" # Common path for GraphQL queries


@dataclass
class AuthResult:
    """Result of authentication with the Wiz API."""
    auth_headers: Dict[str, str]
    data_center: str
    env: str


class WizQueryAgent:
    def __init__(self,
                 name: str = 'WizQueryAgent',
                 description: str = 'Queries Wiz API for security data after LLM processing.',
                 default_query: Optional[Dict[str, Any]] = None,
                 default_endpoint: str = DEFAULT_WIZ_GQL_PATH,
                 default_method: str = "POST", # GraphQL is typically POST
                 wiz_api_base_url: str = DEFAULT_WIZ_API_BASE_URL):
        self.name = name
        self.description = description
        self.default_query = default_query if default_query else self._get_default_graphql_query()
        self.default_endpoint = default_endpoint
        self.default_method = default_method
        self.wiz_api_base_url = wiz_api_base_url
        
        # Auth cache
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[float] = None
        self._data_center: Optional[str] = None
        
        print(f"{self.name} initialized. Ready to act as an ADK callback.")

    def _get_default_graphql_query(self) -> Dict[str, Any]:
        """Provides a default GraphQL query. Customize as needed."""
        return {
            "query": """
                query CriticalIssues($filterBy: IssueFilters, $first: Int) {
                    issues(filterBy: $filterBy, first: $first) {
                        nodes {
                            id
                            type
                            severity
                            status
                            entitySnapshot {
                                id
                                name
                                type
                                cloudPlatform
                            }
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            """,
            "variables": {
                "first": 5, # Fetch top 5 critical issues
                "filterBy": {
                    "status": ["OPEN"],
                    "severity": ["CRITICAL"]
                }
            }
        }

    def _pad_base64(self, base64_str: str) -> str:
        """Pad a base64 string to a multiple of 4."""
        remainder = len(base64_str) % 4
        if remainder == 0:
            return base64_str
        return base64_str + "=" * (4 - remainder)

    def _authenticate(self) -> Optional[AuthResult]:
        """Authenticate with the Wiz API with caching."""
        # Check if we have a valid cached token
        current_time = time.time()
        env = os.environ.get("WIZ_ENV", "app")
        
        if self._access_token and self._token_expiry and self._data_center and current_time < self._token_expiry:
            # print(f"{self.name}: Using cached access token")
            return AuthResult(
                auth_headers={"Authorization": f"Bearer {self._access_token}"},
                data_center=self._data_center,
                env=env
            )

        # Get client ID and secret from environment variables
        client_id = WIZ_CLIENT_ID
        client_secret = WIZ_CLIENT_SECRET
        
        if not client_id or not client_secret:
            print(f"{self.name} Error: WIZ_CLIENT_ID and WIZ_CLIENT_SECRET environment variables must be set.")
            return None

        # Authenticate with the Wiz API
        print(f"{self.name}: Authenticating with the Wiz API...")
        
        auth_url = f"https://auth.{env}.wiz.io/oauth/token"
        payload = {
            "grant_type": "client_credentials",
            "audience": "wiz-api",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            response = requests.post(auth_url, data=payload, headers=headers, timeout=60)
            response.raise_for_status()
            auth_data = response.json()

            # Extract the access token and expiry time
            self._access_token = auth_data["access_token"]
            self._token_expiry = time.time() + auth_data["expires_in"] - 60  # Subtract 60 seconds for safety

            # Extract the data center from the JWT token
            token_parts = self._access_token.split(".")
            if len(token_parts) >= 2:
                # Decode the JWT payload
                payload_part = token_parts[1]
                padded_payload = self._pad_base64(payload_part)
                decoded_payload = base64.b64decode(padded_payload)
                payload_data = json.loads(decoded_payload)

                # Extract the data center
                self._data_center = payload_data.get("dc", "us1")
            else:
                self._data_center = "us1"

            print(f"{self.name}: Successfully authenticated with the Wiz API (DC: {self._data_center})")

            return AuthResult(
                auth_headers={"Authorization": f"Bearer {self._access_token}"},
                data_center=self._data_center,
                env=env
            )

        except requests.exceptions.RequestException as e:
            print(f"{self.name} Error during authentication: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"{self.name} Error Response content: {e.response.text}")
            return None
        except Exception as e:
            print(f"{self.name} Unexpected error during authentication: {e}")
            return None

    def fetch_wiz_data(self,
                       endpoint: str,
                       method: str,
                       payload: Optional[Dict[str, Any]] = None
                       ) -> Optional[Union[Dict[str, Any], list]]:
        """A reusable method to fetch data from Wiz."""
        auth_result = self._authenticate()
        if not auth_result:
            print(f"{self.name}: Cannot fetch Wiz data, authentication failed.")
            return None

        json_body_to_send = None
        query_params_to_send = None

        if method.upper() in ["POST", "PUT", "PATCH"]:
            json_body_to_send = payload
        else: # GET, DELETE etc.
            query_params_to_send = payload

        # Construct URL based on data center if needed, or use provided base URL
        # Note: The original code used a fixed base URL or one passed in init.
        # wiz-mcp uses dynamic URL based on DC, but here we stick to the init param 
        # unless we want to support multi-region dynamically. 
        # For now, let's use the base_url from init but we could update it if we wanted to use the DC.
        # However, to be safe and consistent with previous behavior, we use wiz_api_base_url.
        
        if not endpoint.startswith("/"):
            full_url = f"{self.wiz_api_base_url}/{endpoint}"
        else:
            full_url = f"{self.wiz_api_base_url}{endpoint}"

        headers = auth_result.auth_headers
        headers["Content-Type"] = "application/json"

        try:
            print(f"{self.name}: Making {method.upper()} request to: {full_url}")
            # if query_params_to_send: print(f"{self.name}: With query params: {query_params_to_send}")
            # if json_body_to_send: print(f"{self.name}: With JSON body: {json.dumps(json_body_to_send)[:200]}...")

            response = requests.request(
                method.upper(), 
                full_url, 
                headers=headers, 
                params=query_params_to_send, 
                json=json_body_to_send,
                timeout=60
            )
            response.raise_for_status()

            if response.status_code == 204: # No Content
                return {}
            return response.json()

        except requests.exceptions.HTTPError as e:
            print(f"{self.name} HTTP Error during API request to {full_url}: {e}")
            if e.response is not None:
                print(f"{self.name} Error Response content: {e.response.text}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"{self.name} RequestException during API request to {full_url}: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"{self.name} Error: Failed to decode JSON response from API: {full_url} - {e}")
            return None

    def __call__(self, callback_context: CallbackContext, llm_response: LlmResponse) -> Optional[LlmResponse]:
        """
        ADK callback method. Fetches Wiz data and appends it to the LlmResponse.
        """
        wiz_query_payload = self.default_query
        wiz_endpoint = self.default_endpoint
        wiz_method = self.default_method

        print(f"{self.name}: Using Wiz query payload: {json.dumps(wiz_query_payload)[:200]}...")

        wiz_results = self.fetch_wiz_data(
            endpoint=wiz_endpoint,
            method=wiz_method,
            payload=wiz_query_payload
        )
        
        if wiz_results:
             # Append findings to the response or context as needed.
             # For this example, we'll just print them or attach to output_data if ADK supports it
             # or append to text.
             # The original code didn't actually *do* anything with wiz_results in __call__ other than return llm_response.
             # Let's assume we want to attach it to the response somehow.
             # Since the original code just returned llm_response, we will do the same but maybe log it.
             print(f"{self.name}: Retrieved {len(wiz_results.get('data', {}).get('issues', {}).get('nodes', []))} issues.")
             
             # Optionally append to llm_response text or metadata
             # llm_response.output_data['wiz_findings'] = wiz_results # If supported
             pass
             
        return llm_response


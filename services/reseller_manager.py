"""
Reseller API manager supporting multi-provider architecture.

Compatible with Excalibur Shop Bot API, EM Store, Canboso Telegram Buyer API, and Generic REST Providers:
- Products: GET
- Balance: GET
- Order: POST/GET
- Orders List: GET
- Order Details: GET
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
import aiohttp

logger = logging.getLogger(__name__)


class ResellerAPIError(Exception):
    """Raised when reseller API returns an error."""

    def __init__(
            self,
            message: str = "",
            status_code: int | None = None,
            response_text: str | None = None,
            provider_id: str | None = None,
            status: int | None = None,
            url: str | None = None,
    ):
        self.status_code = status_code if status_code is not None else status
        self.status = self.status_code
        self.response_text = response_text
        self.provider_id = provider_id
        self.url = url

        self.message = str(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"<ResellerAPIError(status={self.status_code}, message={self.message!r})>"


class ResellerManager:
    def __init__(
            self,
            base_url: str | None = None,
            api_key: str | None = None,
            timeout: int = 20,
            provider_config: dict[str, Any] | None = None,
            session: aiohttp.ClientSession | None = None,
    ):
        self.config = provider_config or {}

        if hasattr(self.config, "to_dict"):
            self.config_dict = self.config.to_dict()
        elif isinstance(self.config, dict):
            self.config_dict = self.config
        else:
            self.config_dict = {}

        resolved_base_url = (
                base_url
                or self.config_dict.get("base_url")
                or (getattr(self.config, "base_url", None) if not isinstance(self.config, dict) else None)
                or ""
        )

        resolved_api_key = (
                api_key
                or (getattr(self.config, "api_key", None) if not isinstance(self.config, dict) else None)
                or self.config_dict.get("api_key")
                or ""
        )

        resolved_api_secret = (
                (getattr(self.config, "api_secret", None) if not isinstance(self.config, dict) else None)
                or self.config_dict.get("api_secret")
                or ""
        )

        if not resolved_base_url:
            raise ValueError("Reseller base URL is required.")

        if not resolved_api_key:
            raise ValueError("Reseller API key is required.")

        self.base_url = str(resolved_base_url).strip()
        self.api_key = str(resolved_api_key).strip()
        self.api_secret = str(resolved_api_secret).strip()

        self.provider_id = str(
            self.config_dict.get("id")
            or self.config_dict.get("provider_id")
            or getattr(self.config, "id", None)
            or "default_provider"
        )
        self.provider_name = str(
            self.config_dict.get("name")
            or getattr(self.config, "name", None)
            or "Reseller"
        )

        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._external_session = session is not None
        self._session: aiohttp.ClientSession | None = session

        raw_auth_type = str(
            self.config_dict.get("auth_type")
            or getattr(self.config, "auth_type", None)
            or ""
        ).lower()

        is_em_store = (
                "em_store" in self.provider_id.lower()
                or "ssondigitalworks" in self.base_url
        )
        is_canboso = "canboso.com" in self.base_url.lower()

        if raw_auth_type:
            self.auth_type = raw_auth_type
        elif is_canboso:
            self.auth_type = "query"
        elif is_em_store:
            self.auth_type = "bearer"
        else:
            self.auth_type = "header"

        self.is_hmac_auth = self.auth_type in {
            "hmac", "hmac_sha256", "dujiao_hmac", "dujiao-next", "dujiao_next", "signature"
        }
        self.is_wmemail_open = self.auth_type in {"wmemail_open", "wmemail", "open_api"}
        if self.is_hmac_auth and not self.api_secret:
            raise ValueError("API secret is required for HMAC authentication.")

        self.auth_header_name = str(
            self.config_dict.get("auth_header_name")
            or getattr(self.config, "auth_header_name", None)
            or "X-API-Key"
        )

        self.auth_query_param = str(
            self.config_dict.get("auth_query_param")
            or getattr(self.config, "auth_query_param", None)
                or ("api_key" if self.is_wmemail_open else "key")
        )

    def __repr__(self) -> str:
        return f"<ResellerManager(provider_id={self.provider_id!r}, base_url={self.base_url!r})>"

    def _build_url(self, endpoint: str) -> str:
        """Safely constructs full target URL from base_url and endpoint path."""
        ep = endpoint.strip()
        if ep.startswith("http://") or ep.startswith("https://"):
            return ep

        base = self.base_url.rstrip("/")

        if ep.startswith("/?"):
            ep = ep[1:]

        if ep.startswith("?"):
            return f"{base}{ep}"

        if not ep.startswith("/"):
            ep = f"/{ep}"

        return f"{base}{ep}"

    def _sanitize_text(self, text: str) -> str:
        """Redact sensitive API keys from text or exception output."""
        if not text:
            return ""
        if self.api_key and len(self.api_key) > 3:
            text = text.replace(self.api_key, "***REDACTED***")
        if self.api_secret and len(self.api_secret) > 3:
            text = text.replace(self.api_secret, "***REDACTED***")
        return text

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
            self._external_session = False
        return self._session

    async def close(self) -> None:
        """Close underlying ClientSession resources if created internally."""
        if self._session and not self._session.closed and not self._external_session:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _get_endpoint_and_method(
            self, feature: str, default_endpoint: str, default_method: str = "GET"
    ) -> tuple[str, str]:
        """Resolve endpoint path and HTTP method for a given feature."""
        top_ep = self.config_dict.get(f"{feature}_endpoint")
        top_method = self.config_dict.get(f"{feature}_method")
        if top_ep:
            return str(top_ep), str(top_method or default_method).upper()

        feature_config = self.config_dict.get(feature)
        if isinstance(feature_config, dict):
            ep = feature_config.get("endpoint") or default_endpoint
            m = feature_config.get("method") or default_method
            return str(ep), str(m).upper()
        elif isinstance(feature_config, str):
            return feature_config, default_method

        endpoints = self.config_dict.get("endpoints", {})
        if isinstance(endpoints, dict) and feature in endpoints:
            ep_val = endpoints[feature]
            if isinstance(ep_val, dict):
                ep = ep_val.get("endpoint") or default_endpoint
                m = ep_val.get("method") or default_method
                return str(ep), str(m).upper()
            elif isinstance(ep_val, str):
                return ep_val, default_method

        is_canboso = "canboso.com" in self.base_url.lower()
        if self.is_hmac_auth:
            prefix = "" if "/api/v1/upstream" in self.base_url else "/api/v1/upstream"
            hmac_endpoints = {
                "me": f"{prefix}/ping",
                "balance": f"{prefix}/ping",
                "products": f"{prefix}/products",
                "order": f"{prefix}/orders",
                "order_detail": f"{prefix}/orders/{{id}}",
            }
            return hmac_endpoints.get(feature, default_endpoint), (
                "POST" if feature in {"order", "me", "balance"} else default_method
            )
        elif self.is_wmemail_open:
            open_endpoints = {
                "balance": "/api/v1/open/balance",
                "products": "/api/v1/open/products",
                "order": "/api/v1/open/orders",
            }
            return open_endpoints.get(feature, default_endpoint), (
                "POST" if feature == "order" else default_method
            )
        elif is_canboso:
            if feature == "products":
                return "/api/v2/telegram-buyer/products", default_method
            elif feature == "balance":
                return "/api/v2/telegram-buyer/balance", default_method
            elif feature == "order":
                return "/api/v2/telegram-buyer/purchase", default_method

        return default_endpoint, default_method

    def _get_mapping(self, feature: str, default: Any = None) -> Any:
        """Resolve response field mapping for a given feature."""
        feature_config = self.config_dict.get(feature)
        if isinstance(feature_config, dict) and "mapping" in feature_config:
            return feature_config["mapping"]

        mappings = self.config_dict.get("mapping") or self.config_dict.get("response_mappings")
        if isinstance(mappings, dict):
            if feature in mappings:
                return mappings[feature]
            return mappings

        is_canboso = "canboso.com" in self.base_url.lower()
        if is_canboso and feature == "products":
            return {
                "service_id": "productId",
                "name": "name",
                "price": "price.amount",
                "stock": "availability.available",
                "description": "description",
                "category": "productType"
            }

        return default

    def _extract_value(self, data: Any, key_path: str | None) -> Any:
        """Extract value from nested dictionary using direct key or dot notation."""
        if not data or not key_path:
            return None
        if isinstance(data, dict) and key_path in data:
            return data[key_path]

        parts = key_path.split(".")
        curr = data
        for part in parts:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            else:
                return None
        return curr

    def _clean_product_name_and_id(self, raw_name: str, extracted_id: str | None) -> tuple[str, str | None]:
        """Extracts embedded provider tags from raw product names."""
        if not raw_name:
            return "Unknown Product", extracted_id

        tag_match = re.search(r"\{pe:(\d+)\}", raw_name)
        found_id = extracted_id

        if tag_match:
            if not found_id or found_id == "None":
                found_id = tag_match.group(1)
            clean_name = re.sub(r"\{pe:\d+\}", "", raw_name).strip()
        else:
            clean_name = raw_name.strip()

        clean_name = re.sub(r"^[\s\-:]+|[\s\-:]+$", "", clean_name).strip()
        return clean_name or raw_name, found_id

    async def _request(
            self,
            method: str,
            endpoint: str,
            *,
            feature_name: str = "operation",
            json_data: dict[str, Any] | None = None,
            data: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None,
    ) -> Any:
        url = self._build_url(endpoint)

        req_params = dict(params or {})
        if self.auth_type == "query" or (
            self.is_wmemail_open and feature_name == "balance"
        ):
            req_params[self.auth_query_param] = self.api_key

        headers = {
            "Accept": "application/json",
        }
        body_bytes = b""
        request_json = json_data
        request_data = data
        if self.is_wmemail_open and feature_name == "order" and json_data is not None:
            request_json = dict(json_data)
            request_json.setdefault("api_key", self.api_key)
        if json_data is not None and self.is_hmac_auth:
            body_bytes = json.dumps(
                json_data,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            request_json = None
            request_data = body_bytes
            headers["Content-Type"] = "application/json"
        elif json_data is not None:
            headers["Content-Type"] = "application/json"

        if self.is_hmac_auth:
            timestamp = str(int(time.time()))
            request_path = urlsplit(url).path or "/"
            body_md5 = hashlib.md5(body_bytes).hexdigest()
            sign_string = f"{method.upper()}\n{request_path}\n{timestamp}\n{body_md5}"
            signature = hmac.new(
                self.api_secret.encode("utf-8"),
                sign_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers.update({
                "Dujiao-Next-Api-Key": self.api_key,
                "Dujiao-Next-Timestamp": timestamp,
                "Dujiao-Next-Signature": signature,
            })
        elif self.is_wmemail_open:
            headers["Content-Type"] = "application/json"
        elif self.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.auth_type in ("header", "custom_header", "api_key"):
            headers[self.auth_header_name] = self.api_key

        session = await self._get_session()

        try:
            async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=request_json,
                    data=request_data,
                    params=req_params if req_params else None,
            ) as response:
                text = await response.text()
                sanitized_text = self._sanitize_text(text)
                sanitized_url = self._sanitize_text(url)

                if response.status >= 400:
                    clean_detail = sanitized_text
                    try:
                        parsed_json = json.loads(text)
                        if isinstance(parsed_json, dict):
                            clean_detail = (
                                    parsed_json.get("message")
                                    or parsed_json.get("detail")
                                    or parsed_json.get("error")
                                    or sanitized_text
                            )
                    except Exception:
                        pass

                    err_msg = f"Unable to process {feature_name}: {clean_detail}"

                    logger.error(
                        "Reseller API Request Failed | provider_id=%s | url=%s | status=%s | response=%s",
                        self.provider_id,
                        sanitized_url,
                        response.status,
                        sanitized_text[:500]
                    )

                    raise ResellerAPIError(
                        message=err_msg,
                        status_code=response.status,
                        response_text=sanitized_text,
                        provider_id=self.provider_id,
                        url=sanitized_url,
                    )

                if not text or not text.strip():
                    return None

                try:
                    return json.loads(text)
                except Exception:
                    return sanitized_text

        except ResellerAPIError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "Reseller request timed out | provider_id=%s | timeout_seconds=%s | feature=%s",
                self.provider_id, self.timeout.total, feature_name,
            )
            raise ResellerAPIError(
                message="Provider request timed out. Please try again later.",
                status_code=504,
                provider_id=self.provider_id,
            ) from None
        except aiohttp.ClientError as e:
            logger.error("Reseller Network Client Error | provider_id=%s | error_type=%s | error=%s",
                         self.provider_id, type(e).__name__, self._sanitize_text(str(e)))
            raise ResellerAPIError(
                message=f"Provider Connection Error ({type(e).__name__}): Unable to reach target service.",
                provider_id=self.provider_id,
            )
        except Exception as e:
            logger.error("Reseller System Error | provider_id=%s | error_type=%s | error=%s",
                         self.provider_id, type(e).__name__, self._sanitize_text(str(e)))
            raise ResellerAPIError(
                message=f"System Error ({type(e).__name__}): Request processing failed.",
                provider_id=self.provider_id,
            )
        finally:
            if not self._external_session and self._session:
                await self._session.close()
                self._session = None

    async def get_me(self) -> dict[str, Any]:
        """Fetch account metadata from provider."""
        is_canboso = "canboso.com" in self.base_url.lower()
        default_ep = "/api/v2/telegram-buyer/balance" if is_canboso else "/api/v1/me"
        endpoint, method = self._get_endpoint_and_method("me", default_ep, "GET")
        res = await self._request(method, endpoint, feature_name="me")
        return res if isinstance(res, dict) else {"response": res}

    async def get_balance(self) -> Decimal:
        """Fetch balance from configured endpoint."""
        is_em_store = (
                "em_store" in self.provider_id.lower()
                or "ssondigitalworks" in self.base_url
        )
        is_canboso = "canboso.com" in self.base_url.lower()

        if self.is_hmac_auth:
            prefix = "" if "/api/v1/upstream" in self.base_url else "/api/v1/upstream"
            default_ep = f"{prefix}/ping"
        elif self.is_wmemail_open:
            default_ep = "/api/v1/open/balance"
        elif is_canboso:
            default_ep = "/api/v2/telegram-buyer/balance"
        elif is_em_store:
            default_ep = "?action=balance"
        else:
            default_ep = "/api/v1/me"

        endpoint, method = self._get_endpoint_and_method("balance", default_ep, "GET")

        data = await self._request(method, endpoint, feature_name="balance")

        if not data:
            return Decimal("0.00")

        mapping = self._get_mapping("balance", {})
        balance_key = mapping.get("balance") if isinstance(mapping, dict) else None

        val = None
        if balance_key:
            val = self._extract_value(data, balance_key)

        if val is None and isinstance(data, dict):
            for key in (
            "balance", "funds", "credits", "wallet", "amount", "user_balance", "user.balance", "data.balance", "walletBalance"):
                val = self._extract_value(data, key)
                if val is not None:
                    break

        if val is not None:
            try:
                return Decimal(str(val).strip())
            except (InvalidOperation, TypeError, ValueError):
                pass

        return Decimal("0.00")

    async def get_products(self) -> list[dict[str, Any]]:
        """Fetch complete products catalog from provider."""
        is_em_store = (
                "em_store" in self.provider_id.lower()
                or "ssondigitalworks" in self.base_url
        )
        is_canboso = "canboso.com" in self.base_url.lower()

        if self.is_hmac_auth:
            prefix = "" if "/api/v1/upstream" in self.base_url else "/api/v1/upstream"
            default_ep = f"{prefix}/products"
        elif self.is_wmemail_open:
            default_ep = "/api/v1/open/products"
        elif is_canboso:
            default_ep = "/api/v2/telegram-buyer/products"
        elif is_em_store:
            default_ep = "?action=products"
        else:
            default_ep = "/api/v1/products"

        endpoint, method = self._get_endpoint_and_method("products", default_ep, "GET")

        response = await self._request(method, endpoint, feature_name="products")

        if isinstance(response, dict) and response.get("ok") is False:
            message = response.get("error_message") or response.get("error_code") or "Provider rejected the request."
            raise ResellerAPIError(message=str(message), provider_id=self.provider_id)

        if is_canboso and isinstance(response, dict):
            if response.get("success") is False:
                msg = response.get("message") or response.get("error") or "API returned failure."
                raise ResellerAPIError(message=str(msg), provider_id=self.provider_id)

        raw_list = []
        if isinstance(response, list):
            raw_list = response
        elif isinstance(response, dict):
            if "products" in response and isinstance(response["products"], list):
                raw_list = response["products"]
            else:
                mapping = self._get_mapping("products", {})
                custom_list_key = mapping.get("products_list_key") if isinstance(mapping, dict) else None
                if custom_list_key and custom_list_key in response:
                    raw_list = response[custom_list_key]
                else:
                    for key in ("products", "services", "data", "items", "catalog", "result"):
                        if key in response and isinstance(response[key], list):
                            raw_list = response[key]
                            break
                    if not raw_list:
                        for key, val in response.items():
                            if isinstance(val, list):
                                raw_list = val
                                break

        if not isinstance(raw_list, list):
            sanitized_resp = self._sanitize_text(str(response))
            logger.error(
                "Invalid product catalog format received | provider_id=%s | response_preview=%s",
                self.provider_id,
                sanitized_resp[:300]
            )
            raise ResellerAPIError(
                message="Provider returned an unexpected products response.",
                provider_id=self.provider_id,
            )

        normalized_products = []
        if self.is_hmac_auth or self.is_wmemail_open:
            expanded_items = []
            for item in raw_list:
                skus = item.get("skus") if isinstance(item, dict) else None
                if not isinstance(skus, list):
                    expanded_items.append(item)
                    continue
                title = item.get("title") or item.get("name") or "Product"
                if isinstance(title, dict):
                    title = title.get("en") or title.get("zh-CN") or next(iter(title.values()), "Product")
                for sku in skus:
                    if isinstance(sku, dict) and sku.get("is_active", True):
                        expanded_items.append({
                            "id": sku.get("id"),
                            "name": f"{title} {sku.get('sku_code', '')}".strip(),
                            "price": sku.get("price_amount"),
                            "stock": sku.get("stock_quantity", sku.get("stock_status")),
                            "description": item.get("description"),
                            "category": item.get("category_id"),
                        })
            raw_list = expanded_items
        field_map = self._get_mapping("products", {})
        if not isinstance(field_map, dict):
            field_map = {}

        for item in raw_list:
            if not isinstance(item, dict):
                continue

            sid_key = field_map.get("service_id") or field_map.get("product_id") or field_map.get("id")
            raw_id = self._extract_value(item, sid_key) if sid_key else None
            if raw_id is None:
                for k in ("productId", "service_id", "product_id", "id", "service", "code", "item_id", "ref"):
                    raw_id = self._extract_value(item, k)
                    if raw_id is not None:
                        break

            name_key = field_map.get("name")
            raw_name = self._extract_value(item, name_key) if name_key else None
            if raw_name is None:
                for k in ("name", "title", "product_name", "service_name"):
                    raw_name = self._extract_value(item, k)
                    if raw_name is not None:
                        break

            clean_name, extracted_id = self._clean_product_name_and_id(
                str(raw_name) if raw_name is not None else "",
                str(raw_id) if raw_id is not None else None
            )

            final_service_id = extracted_id or (str(raw_id).strip() if raw_id is not None else None)
            if not final_service_id:
                continue

            price_key = field_map.get("price")
            raw_price = self._extract_value(item, price_key) if price_key else None
            used_price_key = price_key
            if raw_price is None:
                for k in ("price.amount", "price", "rate", "cost", "unit_price"):
                    raw_price = self._extract_value(item, k)
                    if raw_price is not None:
                        used_price_key = k
                        break

            stock_key = field_map.get("stock")
            raw_stock = self._extract_value(item, stock_key) if stock_key else None
            used_stock_key = stock_key
            if raw_stock is None:
                for k in (
                        "availability.available", "quantity", "stock", "qty", "available", "available_stock",
                        "stock_count", "inventory", "count", "units", "in_stock", "balance", "amount"
                ):
                    raw_stock = self._extract_value(item, k)
                    if raw_stock is not None and k != used_price_key:
                        used_stock_key = k
                        break

            desc_key = field_map.get("description")
            raw_desc = self._extract_value(item, desc_key) if desc_key else None
            if raw_desc is None:
                for k in ("description", "desc", "details", "info"):
                    raw_desc = self._extract_value(item, k)
                    if raw_desc is not None:
                        break

            cat_key = field_map.get("category")
            raw_cat = self._extract_value(item, cat_key) if cat_key else None
            if raw_cat is None:
                for k in ("productType", "category", "cat", "type", "group"):
                    raw_cat = self._extract_value(item, k)
                    if raw_cat is not None:
                        break

            try:
                price = Decimal(str(raw_price).strip()) if raw_price is not None else Decimal("0.00")
            except (InvalidOperation, TypeError, ValueError):
                price = Decimal("0.00")

            stock: int = 999999
            is_available: bool = True

            if raw_stock is not None:
                if isinstance(raw_stock, bool):
                    stock = 999999 if raw_stock else 0
                    is_available = raw_stock
                elif isinstance(raw_stock, (int, float)):
                    val = int(raw_stock)
                    if val < 0:
                        stock = 999999
                        is_available = True
                    else:
                        stock = val
                        is_available = stock > 0
                else:
                    s_val = str(raw_stock).strip().lower()
                    if s_val in (
                    "unlimited", "infinite", "infinity", "in_stock", "available", "active", "true", "yes", "enabled"):
                        stock = 999999
                        is_available = True
                    elif s_val in ("out_of_stock", "disabled", "inactive", "false", "no", "none", "null", "oos"):
                        stock = 0
                        is_available = False
                    else:
                        digits = re.findall(r"\d+", s_val)
                        if digits:
                            stock = max(1, int(digits[0])) if s_val == "low_stock" else int(digits[0])
                            is_available = stock > 0
                        else:
                            stock = 999999
                            is_available = True
            else:
                status_val = str(
                    item.get("status") or item.get("available") or item.get("is_available") or "").strip().lower()
                if status_val in ("false", "0", "disabled", "inactive", "out_of_stock", "oos"):
                    stock = 0
                    is_available = False
                else:
                    stock = 999999
                    is_available = True

            final_desc = str(raw_desc).strip() if raw_desc else f"Imported from reseller: {clean_name}"

            normalized_entry = {
                "service_id": str(final_service_id).strip(),
                "provider_product_id": str(final_service_id).strip(),
                "name": clean_name,
                "price": price,
                "provider_cost": price,
                "stock": stock,
                "is_available": is_available,
                "description": final_desc,
                "category": str(raw_cat).strip() if raw_cat else "reseller",
                "raw": item,
            }

            normalized_products.append(normalized_entry)

        return normalized_products

    async def place_order(
            self,
            service_id: str,
            quantity: int = 1,
            external_order_id: str | None = None,
            **kwargs,
    ) -> dict[str, Any]:
        """Place an order with the provider via HTTP POST application/json."""
        is_em_store = (
                "em_store" in self.provider_id.lower()
                or "ssondigitalworks" in self.base_url
        )
        is_canboso = "canboso.com" in self.base_url.lower()

        if self.is_hmac_auth:
            prefix = "" if "/api/v1/upstream" in self.base_url else "/api/v1/upstream"
            default_ep = f"{prefix}/orders"
            payload = {
                "sku_id": int(service_id) if str(service_id).isdigit() else service_id,
                "quantity": int(quantity),
                "downstream_order_no": str(external_order_id or f"ORD-{service_id}-{int(time.time())}"),
            }
            from config import UPSTREAM_CALLBACK_URL

            if UPSTREAM_CALLBACK_URL:
                payload["callback_url"] = UPSTREAM_CALLBACK_URL
        elif self.is_wmemail_open:
            default_ep = "/api/v1/open/orders"
            payload = {
                "sku_id": int(service_id) if str(service_id).isdigit() else service_id,
                "quantity": int(quantity),
            }
        elif is_canboso:
            default_ep = "/api/v2/telegram-buyer/purchase"
            payload = {
                "product_id": str(service_id),
                "quantity": int(quantity),
            }
        elif is_em_store:
            default_ep = "?action=order"
            payload = {
                "service_id": str(service_id),
                "product_id": str(service_id),
                "quantity": int(quantity),
                "qty": int(quantity),
                "external_order_id": str(external_order_id or f"ORD-{service_id}-{int(time.time())}"),
            }
        else:
            default_ep = "/api/v1/order"
            payload = {
                "service_id": str(service_id),
                "product_id": str(service_id),
                "quantity": int(quantity),
                "qty": int(quantity),
                "external_order_id": str(external_order_id or f"ORD-{service_id}-{int(time.time())}"),
            }

        endpoint, method = self._get_endpoint_and_method("order", default_ep, "POST")
        payload.update(kwargs)

        res = await self._request(
            method,
            endpoint,
            feature_name="order",
            json_data=payload,
        )

        if isinstance(res, dict) and (res.get("ok") is False or res.get("success") is False):
            message = res.get("error_message") or res.get("message") or res.get("error_code") or "Provider rejected the order."
            raise ResellerAPIError(message=str(message), provider_id=self.provider_id)

        order_mapping = self._get_mapping("order", {})
        if not isinstance(order_mapping, dict):
            order_mapping = {}

        order_id_key = order_mapping.get("order_id", "order_id")
        status_key = order_mapping.get("status", "status")
        delivery_key = order_mapping.get("delivery", "delivery")

        if isinstance(res, dict):
            order_id = self._extract_value(res, order_id_key) or res.get("order_id") or res.get("id") or res.get("code")
            status = self._extract_value(res, status_key) or res.get("status") or "completed"
            delivery = (
                    self._extract_value(res, delivery_key)
                    or res.get("delivery_items")
                    or res.get("delivery")
                    or res.get("result")
                    or res.get("accounts")
                    or res.get("data")
            )

            result = dict(res)
            result.update({
                "success": True,
                "order_id": str(order_id) if order_id is not None else None,
                "status": str(status) if status is not None else "completed",
                "delivery": delivery,
            })
            return result

        return {
            "success": True,
            "order_id": None,
            "status": "completed",
            "delivery": res,
            "result": res,
        }

    purchase = place_order

    async def get_orders(
            self,
            page: int = 1,
            limit: int = 50,
    ) -> dict[str, Any]:
        """Fetch list of orders from provider."""
        endpoint, method = self._get_endpoint_and_method("orders", "/api/v1/orders", "GET")
        res = await self._request(
            method,
            endpoint,
            feature_name="orders",
            params={"page": page, "limit": limit},
        )
        return res if isinstance(res, dict) else {"orders": res}

    async def get_order(
            self,
            order_id: str,
    ) -> dict[str, Any]:
        """Fetch individual order details by ID."""
        endpoint, method = self._get_endpoint_and_method("order_detail", "/api/v1/order/{id}", "GET")
        if "{id}" in endpoint or "{order_id}" in endpoint:
            formatted_endpoint = endpoint.format(id=order_id, order_id=order_id)
        else:
            formatted_endpoint = f"/api/v1/order/{order_id}"

        res = await self._request(method, formatted_endpoint, feature_name="order_detail")
        return res if isinstance(res, dict) else {"order": res}

    async def get_stats(
            self,
            start: str | None = None,
            end: str | None = None,
    ) -> dict[str, Any]:
        """Fetch sales or usage statistics."""
        endpoint, method = self._get_endpoint_and_method("stats", "/api/v1/stats", "GET")
        params = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        res = await self._request(method, endpoint, feature_name="stats", params=params if params else None)
        return res if isinstance(res, dict) else {"stats": res}

    async def test_connection(self) -> dict[str, Any]:
        """Perform a live connection & authentication test against provider API."""
        try:
            balance = await self.get_balance()
            return {
                "connected": True,
                "provider_id": self.provider_id,
                "provider_name": self.provider_name,
                "balance": float(balance),
                "message": f"Successfully connected to {self.provider_name}",
            }
        except ResellerAPIError as err:
            return {
                "connected": False,
                "provider_id": self.provider_id,
                "provider_name": self.provider_name,
                "error": str(err),
                "status_code": err.status_code,
                "message": f"Connection failed: {str(err)}",
            }
        except Exception as err:
            return {
                "connected": False,
                "provider_id": self.provider_id,
                "provider_name": self.provider_name,
                "error": str(err),
                "message": f"Unexpected error connecting to provider: {str(err)}",
            }

    verify_connection = test_connection

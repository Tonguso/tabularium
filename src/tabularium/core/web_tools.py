"""The tools that reach the web: page fetch, the search services and their quota state, and the agent's browser.
"""
import io
import json
import time
import requests
from tabularium.core.outcome import Failed
from tabularium.memory.memory_store import memory_lock


class WebTools:
    """Part of AgentRuntime (tabularium.core.engine): methods and constants moved here unchanged on 2026-11-23."""

    BROWSER_TOOLS_ANTHROPIC = [
        {
            "name": "browser_close",
            "description": "Close the browser and free all associated resources. Use when done with browser tasks.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "browser_navigate",
            "description": (
                "Open a URL in a headless Chromium browser and return the page's text content. "
                "Browser state persists across tool calls — you can navigate, then click, then type "
                "across multiple calls without losing context. Always navigate before screenshot/click/type."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url":      {"type": "string", "description": "Full URL to navigate to (include https://)."},
                    "authenticated": {"type": "boolean", "description": "Compatibility option. Both values use the normal saved-login browser; no paywall extension is loaded."},
                    "wait_for": {"type": "string", "description": "Wait condition: 'load' (default), 'networkidle', 'domcontentloaded'."},
                },
                "required": ["url"],
            },
        },
        {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page and send it to Alex on Telegram. Use after browser_navigate to show Alex what the page looks like.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "browser_click",
            "description": "Click an element on the current browser page. Returns the page text after the click. Useful for buttons, links, tabs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector or Playwright text selector, e.g. 'button#submit', 'text=Accept', '.nav-link'."},
                },
                "required": ["selector"],
            },
        },
        {
            "name": "browser_type",
            "description": "Type text into an input field on the current browser page. Clears any existing value first.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the input field."},
                    "text":     {"type": "string", "description": "Text to type into the field."},
                },
                "required": ["selector", "text"],
            },
        },
    ]

    TAVILY_SEARCH_TOOL_ANTHROPIC = {
        "name": "web_search",
        "description": "Search the web using Tavily.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
            "required": ["query"],
        },
    }

    TAVILY_SEARCH_TOOL_OPENAI = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using Tavily.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            },
        },
    }

    ANYSEARCH_URL = "https://api.anysearch.com/v1/search"

    def tool_web_fetch(self, url: str, query: str = "") -> str:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            from tabularium.tools.web_content import visible_text, page_result
            text = visible_text(resp.content) if "html" in content_type else resp.text
            return page_result(text, resp.url, self.RUNTIME_DIR / "context_archive" / "results", query)
        except Exception as e:
            return Failed(f"web_fetch failed: {type(e).__name__}: {e}")

    def _tavily_quota_state(self):
        # Shared by foreground/background workers and retained through restart.
        path = self.AGENT_DIR / "task_state" / "search_quota.json"
        return path

    def _tavily_usage_exhausted(self):
        response = requests.get("https://api.tavily.com/usage",
                                headers={"Authorization": "Bearer " + self.TAVILY_API_KEY}, timeout=15)
        response.raise_for_status()
        data = response.json()
        key, account = data.get("key", {}), data.get("account", {})
        limit = key.get("limit")
        key_full = limit is not None and key.get("usage", 0) >= limit
        limit = account.get("plan_limit")
        plan_full = limit is not None and account.get("plan_usage", 0) >= limit
        # Never enable paid Tavily overages ourselves; successful searches confirm recovery.
        return key_full or plan_full

    def tool_web_search_tavily(self, query: str, chat_id: int = 0) -> str:
        path = self._tavily_quota_state()
        try:
            with memory_lock(path):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                blocked = state.get("exhausted", False)
                if blocked and time.time() >= state.get("check_after", 0):
                    try:
                        blocked = self._tavily_usage_exhausted()
                    except requests.RequestException:
                        pass  # Keep native search available if quota lookup is unavailable.
                    state.update(exhausted=blocked, check_after=time.time() + 24 * 60 * 60)
                    self.atomic_json(path, state)
            if blocked:
                return self._search_after_tavily(query, chat_id)
            try:
                response = self.tavily_client.search(query, max_results=5)
            except Exception as exc:
                from tavily.errors import UsageLimitExceededError, ForbiddenError
                if not isinstance(exc, (UsageLimitExceededError, ForbiddenError)):
                    raise
                # Tavily also uses these exceptions for rate limits/forbidden requests.
                # Confirm credit exhaustion instead of treating every error as quota.
                if not self._tavily_usage_exhausted():
                    raise
                with memory_lock(path):
                    self.atomic_json(path, {"exhausted": True, "check_after": time.time() + 24 * 60 * 60})
                return self._search_after_tavily(query, chat_id)
            results = response.get("results", [])
            if not results:
                return "Tavily: no results found."
            return "Search provider: Tavily\n" + "\n".join(
                f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nContent: {r.get('content', '')}\n"
                for r in results)
        except Exception as exc:
            print(f"[search] Tavily failed ({type(exc).__name__}); trying the next engine.")
            try:
                return self._search_after_tavily(query, chat_id)
            except Exception as exc2:
                return f"Search unavailable ({type(exc2).__name__}). Do not treat this as no results."

    def _anysearch_state_path(self):
        return self.AGENT_DIR / "task_state" / "anysearch_state.json"

    def tool_web_search_anysearch(self, query: str):
        """AnySearch (api.anysearch.com), second in the search order (Alex, 2026-10-06):
        Tavily -> AnySearch -> OpenAI native web_search. Returns formatted results, or None when
        unavailable or empty so the caller moves on. Rate/quota/auth refusals pause it briefly
        (task_state/anysearch_state.json) instead of being retried on every search. Works
        anonymously at lower limits; ANYSEARCH_API_KEY in .env raises them."""
        path = self._anysearch_state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("retry_at", 0) > time.time():
            return None
        headers = {"Content-Type": "application/json", "X-Anysearch-Client": "agents/1.0"}
        if self.ANYSEARCH_API_KEY:
            headers["Authorization"] = "Bearer " + self.ANYSEARCH_API_KEY
        try:
            resp = requests.post(self.ANYSEARCH_URL, headers=headers, timeout=30,
                                 json={"query": query, "max_results": 8})
            body = resp.json() if resp.content else {}
        except (requests.RequestException, ValueError) as exc:
            print(f"[search] AnySearch unavailable: {type(exc).__name__}")
            return None
        if not isinstance(body, dict):
            body = {}
        if resp.status_code >= 400 or body.get("code", 0) != 0:
            msg = str(body.get("message") or f"HTTP {resp.status_code}")
            low = msg.lower()
            pause = (3600 if resp.status_code in (401, 403) else
                     1800 if resp.status_code == 429 or any(w in low for w in ("quota", "rate", "limit")) else 0)
            if pause:
                self.atomic_json(path, {"retry_at": time.time() + pause, "reason": msg[:200]})
            print(f"[search] AnySearch refused (HTTP {resp.status_code}): {msg[:120]}")
            return None
        results = (body.get("data") or {}).get("results") or []
        if not results:
            return None
        return "Search provider: AnySearch\n" + "\n".join(
            f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\n"
            f"Content: {r.get('content') or r.get('snippet') or ''}\n" for r in results)

    def _search_after_tavily(self, query: str, chat_id: int) -> str:
        # AnySearch was already tried before Tavily; go on to the model routes.
        return self._native_search(query, chat_id)

    def _native_search(self, query: str, chat_id: int) -> str:
        """Model-native web search in this agent's own route order -- ChatGPT subscription,
        Claude subscription, OpenAI API, Anthropic API, as saved in routing.json. Shared logic in
        D:\\Agents\\search_routing.py."""
        from tabularium.tools import search_routing
        return search_routing.model_search(self, query, chat_id)

    def tool_web_search(self, query: str, chat_id: int = 0) -> str:
        """Engines in the order set on the dashboard (search_order.json, shared by all four agents).
        Default AnySearch -> Tavily -> model routes (Alex, 2026-10-06/07)."""
        from tabularium.tools import search_routing as _search
        for engine in _search.read_order():
            if engine == "anysearch":
                result = self.tool_web_search_anysearch(query)
                if result is not None:
                    return result
            elif engine == "tavily" and self.TAVILY_API_KEY:
                return self.tool_web_search_tavily(query, chat_id)
            elif engine == "model":
                return self._native_search(query, chat_id)
        return "Search unavailable: no engine in the configured order could run (AnySearch returned nothing, Tavily has no key, model routes not in the order)."

    def _browser_shutdown(self) -> None:
        """Clean up Playwright on process exit."""
        pass  # state belongs to the runtime
        try:
            if self._page_instance and not self._page_instance.is_closed():
                self._page_instance.close()
            if self._browser_instance and not self._browser_attached:
                self._browser_instance.close()
            if self._playwright_instance:
                self._playwright_instance.stop()
        except Exception:
            pass
        self._playwright_instance = None
        self._browser_instance    = None
        self._page_instance       = None

    def tool_browser_navigate(self, url: str, wait_for: str = "load", authenticated: bool = False) -> str:
        try:
            with self._browser_lock:
                page = self._get_page(authenticated=authenticated)
                wait_for = wait_for if wait_for in ("load", "networkidle", "domcontentloaded") else "load"
                page.goto(url, wait_until=wait_for, timeout=30000)
                title   = page.title()
                current = page.url
                content = page.evaluate("() => document.body ? document.body.innerText : ''")
                content = content.strip()
                if len(content) > 16000:
                    content = content[:16000] + "\n...[truncated at 16000 chars]"
                return f"Title: {title}\nURL: {current}\n\n{content}"
        except Exception as e:
            return f"Browser navigate error: {e}"

    def tool_browser_screenshot(self, chat_id: int) -> str:
        try:
            with self._browser_lock:
                page = self._get_page()
                data  = page.screenshot(type="png")
            img = io.BytesIO(data)
            img.name = "screenshot.png"
            self.bot.send_photo(chat_id, img)
            return "Screenshot sent."
        except Exception as e:
            return f"Screenshot error: {e}"

    def tool_browser_click(self, selector: str) -> str:
        try:
            with self._browser_lock:
                page = self._get_page()
                page.click(selector, timeout=10000)
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
                title   = page.title()
                current = page.url
                content = page.evaluate("() => document.body ? document.body.innerText : ''")
                content = content.strip()[:4000]
                return f"Clicked '{selector}'.\nTitle: {title}\nURL: {current}\n\n{content}"
        except Exception as e:
            return f"Click error: {e}"

    def tool_browser_close(self) -> str:
        """Explicitly close the browser and free resources."""
        pass  # state belongs to the runtime
        # Other workers can be attached to this headless context.
        with self._browser_lock:
            if self._page_instance and not self._page_instance.is_closed():
                self._page_instance.close()
            self._page_instance = None
        return "This task's browser page closed. Shared authenticated browser remains available."

    def tool_browser_type(self, selector: str, text: str) -> str:
        try:
            with self._browser_lock:
                page = self._get_page()
                page.fill(selector, text, timeout=10000)
                return f"Typed into '{selector}'."
        except Exception as e:
            return f"Type error: {e}"

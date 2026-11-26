# Web search in BAT chat

The `web_search` tool lets your selected model search the web and cite links in its answer. BAT makes the search request using its existing Python environment. Pick one provider for the BAT server. This setting applies to all saved model connections that use Native or JSON tools.

## Choose a provider

**SearXNG.** Use a SearXNG service with JSON output enabled. You need its full search URL. In the PowerShell window used to start BAT, run:

```powershell
$env:BAT_SEARXNG_URL = Read-Host "Full SearXNG search URL, including /search"
```

SearXNG handles queries through its configured engines. Many public instances disable JSON output. [SearXNG Search API](https://docs.searxng.org/dev/search_api.html)

**Brave Search.** Alternatively, supply your Brave Search API subscription token as `BRAVE_SEARCH_API_KEY` in the server's environment. In PowerShell 7.1 or later you can enter it without showing it on screen:

```powershell
$env:BRAVE_SEARCH_API_KEY = Read-Host "Brave Search API key" -MaskInput
```

The model API key and the Brave subscription token serve different services. If both providers are configured, BAT uses SearXNG. A failed request stays with the selected provider. [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)

## Try a search

1. Start or restart BAT from that PowerShell window using your existing interpreter. Under model connection settings, expand **Web search** to see which provider BAT detected. With neither variable set, the tool is off.
2. Open chat with a tool-enabled model and ask, “Search the web for the official Python pathlib documentation. Give me the link and explain what Path represents.”
3. Review the proposed query if prompted. Expand **Search the web** in the conversation to see the titles, snippets and source links returned. The model can use those results in its answer. Open a source to check the full page.

Search sends the query to the configured provider and returns up to five matches. Search and model access are independent: a working model connection can still have an unavailable search service. For a 403 from SearXNG, check JSON access with the operator. For a Brave authentication or quota error, check the search subscription. An empty result list means the service returned no usable matches.

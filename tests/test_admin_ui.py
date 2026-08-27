from __future__ import annotations

import unittest
from pathlib import Path


UI_ROOT = Path(__file__).resolve().parents[1] / "src" / "model_processing_engine" / "admin_ui"


class AdminUiContractTests(unittest.TestCase):
    def test_admin_assets_are_code_native_and_externalized(self) -> None:
        html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (UI_ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertIn('src="/admin/app.js"', html)
        self.assertIn('href="/admin/styles.css"', html)
        self.assertNotIn("<style", html)
        self.assertNotIn("onclick=", html)
        self.assertNotIn("innerHTML", script)
        self.assertIn("setButtonLoading", script)
        self.assertIn('id="provider-preset"', html)
        self.assertIn('id="discover-models"', html)
        self.assertIn('id="models-path"', html)
        self.assertIn('id="model-select"', html)
        self.assertIn('id="max-concurrency"', html)
        self.assertIn('id="native-json-schema"', html)
        self.assertIn('id="history-list"', html)
        self.assertIn('id="refresh-history"', html)
        self.assertIn('id="history-provider-cache-tokens"', html)
        self.assertIn('id="history-provider-cache-coverage"', html)
        self.assertIn('id="history-transport-retries"', html)
        self.assertIn('data-view="providers"', html)
        self.assertIn('data-view="executions"', html)
        self.assertIn('id="execution-view"', html)
        self.assertIn('id="history-anchor"', html)
        self.assertIn('id="history-kind"', html)
        self.assertIn('id="history-provider"', html)
        self.assertIn('id="history-model"', html)
        self.assertIn('id="history-trend"', html)
        self.assertIn('id="history-model-table"', html)
        self.assertIn("MPE 结果缓存", html)
        self.assertIn("availableModels", script)
        self.assertIn("maxConcurrency", script)
        self.assertIn("providerCacheHitExecutions", script)
        self.assertIn("cacheReadInputTokens", script)
        self.assertIn("transportRetries", script)
        self.assertIn("nativeJsonSchema", script)
        self.assertIn("/providers/models", script)
        self.assertIn("/execution-stats?", script)
        self.assertIn("/executions?", script)
        self.assertIn('includeSummary: "false"', script)
        self.assertIn("historyTimezone", script)
        self.assertIn("renderModelBreakdown", script)
        self.assertIn("renderTrend", script)
        self.assertIn("@media (max-width: 780px)", styles)


if __name__ == "__main__":
    unittest.main()

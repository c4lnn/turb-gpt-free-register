# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


TEMPLATE_PATH = Path(__file__).parents[1] / "webui" / "templates" / "index.html"


class AccountFilterToolbarTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_query_and_batch_sections_have_separate_responsibilities(self):
        query_start = self.template.index('id="accountsQuerySectionV2"')
        batch_start = self.template.index('id="accountsBatchSectionV2"')
        query = self.template[query_start:batch_start]
        batch_end = self.template.index('<div class="accounts-table-v2-wrap">', batch_start)
        batch = self.template[batch_start:batch_end]

        self.assertIn("查询与筛选", query)
        self.assertIn('id="qAccountsV2"', query)
        self.assertIn('id="accountEmailSourceFilterV2"', query)
        self.assertIn('id="accountPlanCategoryFilterV2"', query)
        self.assertIn('id="accountCodexAuthFilterV2"', query)
        self.assertNotIn("accountsSelectedHintV2", query)
        self.assertNotIn("codexBulkWorkersV2", query)
        self.assertIn("批量操作", batch)
        self.assertIn("accountsSelectedHintV2", batch)
        self.assertIn("codexBulkWorkersV2", batch)

    def test_filter_fields_stack_labels_above_compact_horizontal_selects(self):
        styles_start = self.template.index(".accounts-filter-v2 {")
        styles_end = self.template.index("/* ---- Codex 授权 ---- */", styles_start)
        styles = self.template[styles_start:styles_end]

        self.assertIn("display: flex", styles)
        self.assertIn("flex-wrap: wrap", styles)
        self.assertIn("flex: 0 1 320px", styles)
        self.assertIn("width: 320px", styles)
        self.assertIn("flex-direction: column", styles)
        self.assertIn("flex: 0 0 auto", styles)
        self.assertIn(".accounts-filter-v2 .acc-v2-select-field > span", styles)
        self.assertIn("width: 132px; height: 34px", styles)
        self.assertIn("#accountPlanCategoryFilterV2", styles)
        self.assertIn("width: 200px", styles)
        self.assertIn("@media (max-width: 700px)", styles)
        self.assertIn("flex-basis: 100%", styles)
        self.assertIn(".accounts-filter-v2 #accountPlanCategoryFilterV2", styles)

    def test_status_codes_are_declared_once_as_filter_metadata(self):
        metadata_start = self.template.index("const ACCOUNT_FILTER_META")
        metadata_end = self.template.index("function accountFilterLabel", metadata_start)
        metadata = self.template[metadata_start:metadata_end]
        for code in (
            "free_trial_eligible", "free_no_trial", "paid", "unknown",
            "not_started", "success", "failed", "skipped",
            "idle", "queued", "running", "canceled",
            "pending", "live", "deactivated", "none",
            "oaics", "cs_live", "other_cs",
            "outlook", "generic_api", "cloudflare_domain", "icloud", "cloudflare",
            "gptmail", "mailnest", "cloudmail", "mailcom",
        ):
            self.assertIn(code, metadata)
        self.assertIn("labelOverrides: Object.freeze({none: '未检测'})", metadata)

    def test_list_and_poll_build_from_the_same_query_function(self):
        load_start = self.template.index("function loadAccounts()")
        poll_start = self.template.index("async function pollAccountPlanStatuses()")
        load_end = self.template.index("async function pollAccountPlanStatuses()", load_start)
        poll_end = self.template.index("const ACCOUNT_STATUS_LABELS", poll_start)
        load = self.template[load_start:load_end]
        poll = self.template[poll_start:poll_end]
        self.assertIn("const params = buildAccountsQueryParams();", load)
        self.assertIn("const params = buildAccountsQueryParams();", poll)
        self.assertIn("/api/accounts?${params.toString()}", load)
        self.assertIn("/api/accounts/plan-check-status?${params.toString()}", poll)
        self.assertIn("date_from", self.template[self.template.index("function buildAccountsQueryParams()"):poll_end])
        self.assertNotIn("SHOW_ARCHIVED_ACCOUNTS", self.template)
        self.assertNotIn("SHOW_PLUS_ACCOUNTS_ONLY", self.template)
        self.assertNotIn("CHECKOUT_TYPE_FILTER", self.template)

    def test_filter_change_clears_selection_resets_page_and_queues_reload(self):
        start = self.template.index("async function reloadAccountsForFilterChange()")
        end = self.template.index("async function refreshAccountsList", start)
        handler = self.template[start:end]
        self.assertIn("accountsFilterVersion += 1", handler)
        self.assertIn("ACCOUNT_SELECTED.clear()", handler)
        self.assertIn("PAGERS.accounts.page = 1", handler)
        self.assertIn("planStatusRevision = ''", handler)
        self.assertIn("updateAccountSelectionUi([])", handler)
        self.assertIn("await loadAccounts()", handler)
        self.assertIn("await pollAccountPlanStatuses()", handler)

    def test_every_filter_select_uses_machine_code_values_and_change_binding(self):
        for element_id, state_key in (
            ("accountArchiveFilterV2", "archived"),
            ("accountEmailSourceFilterV2", "email_source"),
            ("accountPlanCategoryFilterV2", "plan_category"),
            ("accountCodexAuthFilterV2", "codex_auth_status"),
            ("accountCodexOperationFilterV2", "codex_operation_status"),
            ("accountLiveCheckFilterV2", "live_check_status"),
            ("checkoutTypeFilterV2", "checkout_type"),
        ):
            self.assertIn(f'id="{element_id}" data-account-filter="{state_key}"', self.template)
        binding_start = self.template.index("(function bindAccountsFilterV2()")
        binding_end = self.template.index("async function fetchAccountSecrets", binding_start)
        binding = self.template[binding_start:binding_end]
        self.assertIn("select.addEventListener('change'", binding)
        self.assertIn("applyAccountsFilterChange(select.dataset.accountFilter, select.value)", binding)
        self.assertIn("bindDateFilterPanel", binding)

    def test_account_source_display_uses_central_label_and_preserves_raw_code(self):
        render_start = self.template.index("function renderAccounts()")
        render_end = self.template.index("function updateAccountSelectionUi", render_start)
        renderer = self.template[render_start:render_end]
        self.assertIn("_statusLabel('emailSource', r.email_source)", renderer)
        self.assertIn("title=\"${esc(r.email_source || 'unknown')}\"", renderer)

    def test_batch_workers_stay_in_operation_context(self):
        query_start = self.template.index('id="accountsQuerySectionV2"')
        batch_start = self.template.index('id="accountsBatchSectionV2"')
        query = self.template[query_start:batch_start]
        self.assertNotIn("workers", query)

        workers_start = self.template.index("function getCodexBulkWorkers()")
        workers_end = self.template.index("function loadAccounts()", workers_start)
        workers = self.template[workers_start:workers_end]
        self.assertIn("Number.parseInt", workers)
        self.assertIn("Math.max(1, Math.min(16", workers)
        self.assertIn(": 3", workers)

        self.assertIn("body: JSON.stringify({account_ids: ids, workers})", self.template)
        checkout_start = self.template.index("async function checkSelectedCheckoutSessions()")
        checkout_end = self.template.index("async function checkSelectedLive", checkout_start)
        checkout = self.template[checkout_start:checkout_end]
        self.assertIn("/api/accounts/check-checkout-session-bulk", checkout)
        self.assertNotIn("workers", checkout)

    def test_capability_driven_batch_buttons_cover_live_and_extract_status(self):
        start = self.template.index("function updateAccountSelectionUi")
        end = self.template.index("// ---------- 补跑日志面板 ----------", start)
        selection = self.template[start:end]
        self.assertIn("btnCheckSelectedLiveV2", selection)
        self.assertIn("row.live_check_capabilities?.can_start === true", selection)
        self.assertIn("row.plan_capabilities?.is_eligible === true", selection)
        self.assertIn("row.extract_link_capabilities?.can_start === true", selection)
        self.assertIn("row.plan_capabilities?.can_start === true", selection)
        self.assertIn("row.checkout_capabilities?.can_retry === true", selection)
        self.assertIn("row.codex_capabilities?.can_retry === true", selection)
        self.assertIn("btnStopSelectedCodexV2", selection)

    def test_row_actions_use_capabilities_and_normalized_codes(self):
        plan_start = self.template.index("function _planAction")
        plan_end = self.template.index("function _fmtExtractExpire", plan_start)
        plan = self.template[plan_start:plan_end]
        self.assertIn("capabilities.is_checking === true", plan)
        self.assertIn("capabilities.can_start !== true", plan)

        checkout_start = self.template.index("function _checkoutSessionAction")
        checkout_end = self.template.index("function _checkoutSessionCell", checkout_start)
        checkout = self.template[checkout_start:checkout_end]
        self.assertIn("capabilities.is_checking === true", checkout)
        self.assertIn("const hasAccessToken = capabilities.has_access_token === true;", checkout)
        self.assertIn("capabilities.can_retry !== true", checkout)
        self.assertNotIn("r.has_access_token", checkout)

        extract_start = self.template.index("function _extractLinkAction")
        extract_end = self.template.index("function _codexAction", extract_start)
        extract = self.template[extract_start:extract_end]
        self.assertIn("capabilities.is_running === true", extract)
        self.assertIn("capabilities.resumable === true", extract)
        self.assertIn("capabilities.can_start !== true", extract)

        codex_start = self.template.index("function _codexAction")
        codex_end = self.template.index("function _tokenCellV2", codex_start)
        codex = self.template[codex_start:codex_end]
        self.assertIn("capabilities.can_stop === true", codex)
        self.assertIn("capabilities.can_retry !== true", codex)

    def test_action_handlers_recheck_capabilities_before_requests(self):
        plan_start = self.template.index("async function checkOnePlan")
        plan_end = self.template.index("async function checkOneCheckoutSession", plan_start)
        self.assertIn("account?.plan_capabilities?.can_start !== true", self.template[plan_start:plan_end])

        checkout_start = self.template.index("async function checkOneCheckoutSession")
        checkout_end = self.template.index("async function extractOneLink", checkout_start)
        self.assertIn("account?.checkout_capabilities?.can_retry !== true", self.template[checkout_start:checkout_end])

        resume_start = self.template.index("async function resumeExtractLink")
        resume_end = self.template.index("async function stopSelectedCodex", resume_start)
        resume = self.template[resume_start:resume_end]
        self.assertIn("acc.extract_link_capabilities?.resumable !== true", resume)
        self.assertIn("acc.extract_link_capabilities?.can_retry !== true", resume)

        stop_start = self.template.index("async function stopSelectedCodex")
        retry_start = self.template.index("async function retrySelectedCodex", stop_start)
        self.assertIn("a.codex_capabilities?.can_stop === true", self.template[stop_start:retry_start])
        retry_end = self.template.index("async function copyCurrentPageTokens", retry_start)
        self.assertIn("a.codex_capabilities?.is_running === true", self.template[retry_start:retry_end])
        self.assertIn("a.codex_capabilities?.can_retry !== true", self.template[retry_start:retry_end])


if __name__ == "__main__":
    unittest.main()

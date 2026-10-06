from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from main import _storage_csp_sources


ROOT = Path(__file__).resolve().parents[1]


class PortalContractTest(unittest.TestCase):
    def test_music_status_does_not_report_expired_login_as_service_outage(self) -> None:
        app_js = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        creative_js = (ROOT / "portal" / "creative.js").read_text(encoding="utf-8")
        self.assertIn("error.status = response.status", app_js)
        self.assertIn("error.status = xhr.status", app_js)
        self.assertIn("if (response.status === 401) handleSessionExpired();", app_js)
        self.assertIn("function handleSessionExpired()", app_js)
        self.assertIn('$("#loginForm").addEventListener("input"', app_js)
        self.assertIn("authExpired: error.status === 401", creative_js)
        self.assertIn("登录已失效，请重新登录后使用音乐编辑。", creative_js)
        self.assertIn("const musicErrorMessage = error => error?.status === 401", creative_js)

    def test_digital_human_price_is_displayed_per_fifteen_second_segment(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "creative.js").read_text(encoding="utf-8")
        self.assertIn("每 15 秒 1.000 pt，不足 15 秒按 1 段计", page)
        self.assertIn("每 15 秒 ${digitalHumanPricePerSegment.toFixed(3)} pt", script)
        self.assertIn("const billableDuration = completeWindows > 0 && tail > 0 && tail < 1", script)

    def test_digital_human_prompt_is_optional_and_is_merged_into_the_h3_template(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "creative.js").read_text(encoding="utf-8")
        docs = (ROOT / "portal" / "docs.html").read_text(encoding="utf-8")
        digital_human = page[page.index('id="page-digital-human"'):page.index('id="page-editor"')]
        self.assertIn('id="digitalHumanPrompt" maxlength="30000"', digital_human)
        self.assertIn("填写后会与模板整合成一份完整提示词；留空则按原模板生成", digital_human)
        self.assertIn('<option value="16:9">横屏 · 16:9</option>', digital_human)
        self.assertIn('<option value="9:16">竖屏 · 9:16</option>', digital_human)
        submit = script[script.index('$("#digitalHumanForm").addEventListener("submit"'):script.index('$("#imageEditForm").addEventListener("submit"')]
        self.assertIn('form.append("prompt", $("#digitalHumanPrompt").value)', submit)
        self.assertIn("由 DeepSeek 与 H3 模板整合", docs)
        self.assertIn("留空逐字沿用默认模板", docs)

    def test_commerce_creation_forms_do_not_require_notes(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "creative.js").read_text(encoding="utf-8")
        for field in ("remixNote", "editNote"):
            self.assertNotIn(field, page)
            self.assertNotIn(field, script)
        self.assertIn('id="studioNote"', page)
        self.assertIn('creative-note', script)
        self.assertIn('/creative.js?v=direct-upload-20261006-r6', page)
        self.assertIn('/app.js?v=preview-speed-20261006-r8', page)
        self.assertIn('data-open-app="digital-human"', page)
        self.assertIn('id="page-digital-human"', page)
        digital_human = page[page.index('id="page-digital-human"'):page.index('id="page-editor"')]
        self.assertIn('id="digitalHumanAudio"', digital_human)
        self.assertNotIn('id="digitalHumanEmotion"', digital_human)
        self.assertNotIn('id="digitalHumanScript"', digital_human)
        self.assertNotIn('value="text"', digital_human)
        self.assertIn('/creative.css?v=r112-commerce-depth', page)
        remix = page[page.index('id="page-remix"'):page.index('id="page-image-edit"')]
        self.assertIn("remix（支持视频编辑）", remix)
        self.assertIn("720P · 1.5 pt", remix)
        self.assertNotIn("1080P", remix)
        self.assertNotIn("2K", remix)
        self.assertIn('id="remixQuality" disabled', remix)
        self.assertIn('value.remix?.prices?.[option.value]', script)
        self.assertNotIn("Qwen-VL", page)
        self.assertIn("请至少选择一张人物、商品或场景参考图", script)
        for template in ("person", "product", "scene", "person_product", "all"):
            self.assertIn(f'value="{template}"', page)
        self.assertNotIn('value="custom"', page)
        self.assertNotIn("自由组合", page)
        self.assertNotIn('id="remixPrompt"', page)
        self.assertNotIn('$("#remixPrompt")', script)
        self.assertIn('id="remixTemplate"', page)
        self.assertIn('card.hidden = !required', script)
        self.assertIn('form.append("remix_template", templateKey)', script)
        self.assertIn('form.append("remix_audio", "original")', script)
        self.assertNotIn('id="remixAudio"', page)
        self.assertIn('prepareStudioDirectUpload(form', script)
        self.assertIn('$("#remixError"), true)', script)
        self.assertIn("const missing =", script)
        self.assertNotIn("const unused =", script)
        self.assertIn('/api/account/submissions/${encodeURIComponent(key)}', script)
        self.assertIn('上传响应中断，正在确认任务是否已创建', script)
        self.assertIn('compactEditImage', script)
        self.assertIn('button.textContent = "正在优化图片…"', script)
        self.assertIn('id="editImages" type="file" accept="image/*,.heic,.heif" multiple', page)
        self.assertNotIn('id="editImages" type="file" accept="image/*,.heic,.heif" multiple required', page)
        self.assertNotIn('id="editReferences"', page)
        self.assertNotIn('$("#editReferences")', script)
        self.assertNotIn('i === 0 ? " · 原图" : " · 参考"', script)
        self.assertIn('caption.textContent = `图${index + 1} · ${file.name}`', script)

    def test_application_center_contains_all_public_quality_tiers(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        for quality in ("720p", "1080p", "2k"):
            self.assertIn(f'value="{quality}"', page)
        self.assertIn("/api/account/videos/${quality}", script)
        self.assertIn('id="embeddedDocs"', page)
        self.assertIn('fetch("/docs.html', script)
        self.assertIn("new EventSource", script)
        self.assertIn("/api/account/uploads/presign", script)
        self.assertIn('id="taskProcessedPrompt"', page)
        self.assertIn("item.processed_prompt", script)

    def test_application_center_polls_all_active_tasks_even_with_event_streams(self) -> None:
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        refresh = script[
            script.index("async function refreshStudioTaskCenter()"):
            script.index("function resumeStudioTask()")
        ]
        self.assertIn("const STUDIO_ACTIVE_REFRESH_MS = 3000", script)
        self.assertIn("studioTaskItems.some(studioTaskIsActive)", script)
        self.assertIn("scheduleStudioTaskRefresh();", refresh)
        self.assertNotIn('typeof EventSource === "undefined"', refresh)
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        self.assertIn('/app.js?v=preview-speed-20261006-r8', page)
        self.assertIn('/styles.css?v=preview-speed-20261006-r4', page)
        self.assertIn('id="themeToggle"', page)
        self.assertIn('data-overview-record="calls"', page)
        self.assertIn('data-overview-record="ledger"', page)

    def test_editor_has_per_app_icons_video_preview_and_bounded_reference_grid(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "creative.js").read_text(encoding="utf-8")
        styles = (ROOT / "portal" / "creative.css").read_text(encoding="utf-8")
        for image in ("video-generation.webp", "commerce-remix.webp", "digital-human.webp", "video-edit.webp", "image-edit.webp", "music-edit.webp"):
            self.assertIn(f'/app-art/{image}', page)
            self.assertTrue((ROOT / "portal" / "app-art" / image).is_file())
        self.assertIn('id="appRecommendForm"', page)
        self.assertIn('id="appRecommendResults"', page)
        self.assertIn('video.controls = true', script)
        self.assertIn('video.src = url', script)
        self.assertIn('video.load()', script)
        self.assertIn('video.addEventListener("error"', script)
        self.assertIn('每 15 秒 ${editorPricePer15Seconds} pt', script)
        self.assertIn('Math.ceil((seconds - .1) / 15)', script)
        self.assertIn('必选 · 至少 5 秒', page)
        self.assertIn('grid-template-columns: repeat(3, minmax(0, 1fr))', styles)
        self.assertIn('object-fit:contain', styles)
        self.assertIn('overflow:hidden', styles)

    def test_application_center_can_delete_terminal_tasks_and_marks_success_blue(self) -> None:
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "portal" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-delete-studio-task=', script)
        self.assertIn('async function deleteStudioTask(', script)
        self.assertIn('{method:"DELETE"}', script)
        self.assertIn('.studio-task-card.is-complete', styles)
        self.assertIn('.studio-result.is-complete .result-progress-rail i', styles)

    def test_selected_completed_task_plays_in_main_stage_with_compact_poster_cards(self) -> None:
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        polish = (ROOT / "portal" / "app-polish.css").read_text(encoding="utf-8")
        self.assertIn('id="studioResultStage"', (ROOT / "portal" / "index.html").read_text(encoding="utf-8"))
        self.assertIn('function loadStudioTaskPoster(task, card)', script)
        self.assertIn('thumb.prepend(image)', script)
        self.assertIn('video.src = artifact.preview_url || artifact.download_url', script)
        self.assertIn('grid-template-columns:repeat(3,minmax(0,1fr))', polish)
        self.assertNotIn('studioInlinePreview', script)
        self.assertNotIn('selectedCard.appendChild', script)
        self.assertIn('studioSelectedTaskDetail = task', script)

    def test_video_notes_replace_prompt_titles_without_replacing_prompt_data(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="studioNote"', page)
        self.assertIn('task.note || task.prompt_preview', script)
        self.assertIn('data-note-studio-task=', script)
        self.assertIn('method:"PATCH"', script)
        self.assertIn('form.append("note"', script)

    def test_overview_contains_the_single_call_history_surface(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(page.count('id="callTable"'), 1)
        self.assertEqual(page.count('id="callFilter"'), 1)
        self.assertIn('id="callHistory"', page)
        self.assertNotIn('id="page-calls"', page)
        self.assertNotIn('data-page="calls"', page)
        self.assertIn('name === "calls" || name === "ledger"', script)
        self.assertIn('item.note || (commerceTask ? item.title : item.prompt_preview', script)

    def test_console_groups_overview_apps_and_api_access(self) -> None:
        page = (ROOT / "portal" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        polish = (ROOT / "portal" / "app-polish.css").read_text(encoding="utf-8")
        self.assertIn('data-page="overview"', page)
        self.assertIn('data-page="apps"', page)
        self.assertIn('data-page="api"', page)
        self.assertNotIn('data-page="ledger"', page)
        self.assertNotIn('data-page="keys"', page)
        self.assertNotIn('data-page="docs"', page)
        overview = page[page.index('id="page-overview"'):page.index('id="page-apps"')]
        self.assertIn('id="pointsLedger"', overview)
        self.assertIn('id="ledgerTable"', overview)
        self.assertNotIn('id="page-ledger"', page)
        apps = page[page.index('id="page-apps"'):page.index('id="page-commerce-remix"')]
        for tool in ("studio", "commerce-remix", "digital-human", "editor", "image-edit", "music"):
            self.assertIn(f'data-open-app="{tool}"', apps)
        self.assertNotIn('data-open-app="remix"', apps)
        self.assertNotIn('data-open-app="remix2"', apps)
        self.assertIn('6 个工具', apps)
        self.assertNotIn('data-open-feature-guide', apps)
        self.assertIn('repeat(auto-fit,minmax(min(100%,230px),1fr))', polish)
        self.assertIn('content:none !important; display:none !important', polish)
        self.assertIn('#consoleView #page-studio .task-card-thumb video', polish)
        api = page[page.index('id="page-api"'):page.index('id="communityModal"')]
        self.assertIn('id="apiSectionKeys"', api)
        self.assertIn('id="apiSectionDocs"', api)
        self.assertIn('id="createKeyBtn"', api)
        self.assertIn('id="embeddedDocs"', api)
        self.assertIn('function switchApiTab(name)', script)
        self.assertIn('if (name === "keys") { activeApiTab = "keys"; name = "api"; }', script)
        self.assertIn('ledgerLimit = Math.min(200, ledgerLimit + 20)', script)

    def test_call_details_and_download_are_separate_actions(self) -> None:
        script = (ROOT / "portal" / "app.js").read_text(encoding="utf-8")
        self.assertIn(">查看</button>", script)
        self.assertIn(">下载</button>", script)
        self.assertNotIn(">详情 / 下载</button>", script)
        self.assertIn("async function downloadTask(id, button)", script)
        self.assertIn('api(`/api/account/tasks/${encodeURIComponent(id)}`)', script)

    def test_public_portal_does_not_expose_internal_video_pipeline(self) -> None:
        public_copy = "\n".join(
            (ROOT / "portal" / name).read_text(encoding="utf-8")
            for name in ("index.html", "docs.html", "app.js")
        ).lower()
        for internal_term in (
            "逐级",
            "latent",
            "denoise",
            "sageattention",
            "solattn",
            "sigma refiner",
            "1280×736 →",
        ):
            self.assertNotIn(internal_term, public_copy)

    def test_public_config_does_not_describe_internal_processing(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        config = source[source.index('@app.get("/api/public/config")'):]
        config = config[:config.index("portal =")].lower()
        for internal_term in ("first_pass", "refine_targets", '"passes"', "latent", "denoise"):
            self.assertNotIn(internal_term, config)

    def test_docs_cover_all_three_quality_helpers_and_prices(self) -> None:
        docs = (ROOT / "portal" / "docs.html").read_text(encoding="utf-8")
        self.assertIn("def create_720p", docs)
        self.assertIn("def create_1080p", docs)
        self.assertIn("def create_2k", docs)
        self.assertIn("失败全额退回", docs)

    def test_docs_list_only_the_additional_console_creation_apis(self) -> None:
        docs = (ROOT / "portal" / "docs.html").read_text(encoding="utf-8")
        section = docs[docs.index('id="app-apis"'):docs.index('id="python"')]
        for endpoint in (
            "/api/account/commerce-remix/720p",
            "/api/account/digital-human/720p",
            "/api/account/video-edits/720p",
            "/api/account/image-edits",
            "/api/account/music",
        ):
            self.assertIn(endpoint, section)
        self.assertIn("控制台 Cookie", section)
        self.assertIn("清晰度固定为 1M", section)
        self.assertNotIn("/api/account/videos/", section)
        self.assertNotIn("/v1/videos/", section)

    def test_video_playback_csp_only_allows_configured_storage_origins(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "COS_BUCKET": "video-1234567890",
                "COS_REGION": "ap-guangzhou",
                "COS_CDN_DOMAIN": "https://media.example.com",
            },
            clear=False,
        ):
            self.assertEqual(
                _storage_csp_sources(),
                [
                    "https://video-1234567890.cos.ap-guangzhou.myqcloud.com",
                    "https://media.example.com",
                ],
            )

        with patch.dict(
            "os.environ",
            {"COS_BUCKET": "bad; media-src *", "COS_REGION": "ap-guangzhou"},
            clear=False,
        ):
            self.assertNotIn("https://bad; media-src *", _storage_csp_sources())


if __name__ == "__main__":
    unittest.main()

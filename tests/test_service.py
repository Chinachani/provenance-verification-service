import os
import struct
import unittest
import zlib
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SERVICE_API_TOKEN", "t" * 48)

import app
from fastapi import HTTPException


class ImageTypeTests(unittest.TestCase):
    def test_accepts_jpeg_png_and_webp_signatures(self):
        self.assertEqual(app.detect_image_type("image/jpeg", b"\xff\xd8\xffdata"), "image/jpeg")
        self.assertEqual(app.detect_image_type("image/png", b"\x89PNG\r\n\x1a\ndata"), "image/png")
        self.assertEqual(app.detect_image_type("image/webp", b"RIFFxxxxWEBPdata"), "image/webp")

    def test_rejects_type_mismatch_and_unsupported_type(self):
        for content_type, data in (("image/jpeg", b"nope"), ("image/gif", b"GIF89a")):
            with self.subTest(content_type=content_type), self.assertRaises(HTTPException):
                app.detect_image_type(content_type, data)


class StatusTests(unittest.TestCase):
    def test_no_signal_is_not_a_claim_that_content_is_not_ai(self):
        self.assertEqual(
            app.overall_status({"state": "not_found"}, {"state": "completed", "results": []}),
            "no_supported_signal_found",
        )

    def test_failed_check_is_distinct_from_no_signal(self):
        self.assertEqual(
            app.overall_status({"state": "not_found"}, {"state": "failed"}),
            "detection_failed_or_unsupported",
        )

    def test_invalid_manifest_is_not_reported_as_verified(self):
        self.assertEqual(
            app.overall_status({"state": "unverified_claim"}, None), "unverified_claim_found"
        )

    def test_openai_positive_is_not_hidden_by_unverified_c2pa(self):
        self.assertEqual(
            app.overall_status(
                {"state": "unverified_claim"},
                {"state": "completed", "results": [{"outcome": "detected", "type": "synthid"}]},
            ),
            "unverified_claim_found",
        )

    def test_real_c2pa_sdk_reports_clean_image_without_manifest_as_no_signal(self):
        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
            + chunk(b"IEND", b"")
        )
        result = app.read_c2pa("image/png", png)
        self.assertEqual(result["state"], "not_found")

    def test_c2pa_validation_uses_top_level_sdk_state(self):
        manifest_id = "urn:example:manifest"
        base = {"active_manifest": manifest_id, "manifests": {manifest_id: {"title": "image"}}}
        cases = {
            "Trusted": "verified",
            "Valid": "unverified_claim",
            "Invalid": "unverified_claim",
            "NotPresent": "not_found",
            None: "unavailable",
        }
        for validation_state, expected in cases.items():
            with self.subTest(validation_state=validation_state):
                report = {**base, "validation_state": validation_state}
                self.assertEqual(app.summarize_c2pa_report(report)["state"], expected)


class ApiTests(unittest.TestCase):
    def test_health_and_authentication_and_size_bound(self):
        from fastapi.testclient import TestClient

        with TestClient(app.app) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            unauthorized = client.post(
                "/v1/verify", content=b"\xff\xd8\xffx", headers={"content-type": "image/jpeg"}
            )
            self.assertEqual(unauthorized.status_code, 401)

            with patch.object(app, "MAX_IMAGE_BYTES", 4):
                oversized = client.post(
                    "/v1/verify",
                    content=b"\xff\xd8\xffxx",
                    headers={
                        "authorization": f"Bearer {app.API_TOKEN}",
                        "content-type": "image/jpeg",
                    },
                )
                self.assertEqual(oversized.status_code, 413)

    def test_authenticated_upload_returns_safe_status_without_openai_by_default(self):
        from fastapi.testclient import TestClient

        with patch.object(
            app,
            "read_c2pa",
            return_value={"state": "verified", "manifest": {"title": "sample"}},
        ) as c2pa_reader, patch.object(app, "check_openai") as openai_check:
            with TestClient(app.app) as client:
                response = client.post(
                    "/v1/verify",
                    content=b"\xff\xd8\xffimage-bytes",
                    headers={
                        "authorization": f"Bearer {app.API_TOKEN}",
                        "content-type": "image/jpeg",
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "verified_source")
            self.assertIsNone(response.json()["openai_provenance"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            c2pa_reader.assert_called_once()
            openai_check.assert_not_called()

    def test_openai_key_is_request_scoped_and_never_returned(self):
        from fastapi.testclient import TestClient

        api_key = "sk-test-per-request-secret"
        with patch.object(app, "read_c2pa", return_value={"state": "not_found"}), patch.object(
            app,
            "check_openai",
            new=AsyncMock(return_value={"state": "completed", "results": []}),
        ) as openai_check:
            with TestClient(app.app) as client:
                response = client.post(
                    "/v1/verify?include_openai=true",
                    content=b"\xff\xd8\xffimage-bytes",
                    headers={
                        "authorization": f"Bearer {app.API_TOKEN}",
                        "x-openai-api-key": api_key,
                        "content-type": "image/jpeg",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_supported_signal_found")
        self.assertNotIn(api_key, response.text)
        openai_check.assert_awaited_once_with(b"\xff\xd8\xffimage-bytes", "image/jpeg", api_key)

    def test_openai_check_without_request_key_is_not_sent_upstream(self):
        from fastapi.testclient import TestClient

        with patch.object(app, "read_c2pa", return_value={"state": "not_found"}), patch.object(
            app, "check_openai", new=AsyncMock()
        ) as openai_check:
            with TestClient(app.app) as client:
                response = client.post(
                    "/v1/verify?include_openai=true",
                    content=b"\xff\xd8\xffimage-bytes",
                    headers={
                        "authorization": f"Bearer {app.API_TOKEN}",
                        "content-type": "image/jpeg",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["openai_provenance"],
            {"state": "unavailable", "reason": "missing_request_key"},
        )
        openai_check.assert_not_awaited()

    def test_fallback_calls_openai_when_c2pa_is_missing(self):
        from fastapi.testclient import TestClient

        api_key = "sk-test-fallback"
        with patch.object(app, "read_c2pa", return_value={"state": "not_found"}), patch.object(
            app,
            "check_openai",
            new=AsyncMock(return_value={"state": "completed", "results": []}),
        ) as openai_check:
            with TestClient(app.app) as client:
                response = client.post(
                    "/v1/verify?openai_fallback=true",
                    content=b"\xff\xd8\xffimage-bytes",
                    headers={
                        "authorization": f"Bearer {app.API_TOKEN}",
                        "x-openai-api-key": api_key,
                        "content-type": "image/jpeg",
                    },
                )
        self.assertEqual(response.status_code, 200)
        openai_check.assert_awaited_once_with(b"\xff\xd8\xffimage-bytes", "image/jpeg", api_key)

    def test_fallback_skips_openai_for_trusted_attributed_c2pa(self):
        from fastapi.testclient import TestClient

        with patch.object(
            app,
            "read_c2pa",
            return_value={"state": "verified", "manifest": {"issuer": "Trusted Issuer"}},
        ), patch.object(app, "check_openai", new=AsyncMock()) as openai_check:
            with TestClient(app.app) as client:
                response = client.post(
                    "/v1/verify?openai_fallback=true",
                    content=b"\xff\xd8\xffimage-bytes",
                    headers={
                        "authorization": f"Bearer {app.API_TOKEN}",
                        "content-type": "image/jpeg",
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["openai_provenance"],
            {"state": "skipped", "reason": "trusted_c2pa_source"},
        )
        openai_check.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

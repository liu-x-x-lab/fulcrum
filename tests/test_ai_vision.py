"""视觉模型与引用媒体的定向回归；只使用虚构数据和本地模拟模型。"""

import base64
import io
import json

import pytest
from PIL import Image

from app.services import ai_dispatch, ai_providers
from app.services.message_view import parse_quote_payload
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.plugin_runtime.text_generation import PluginTextGenerationClient
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed


def image_bytes(color="red", size=(64, 64)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_visual_configuration_does_not_reuse_chat_credentials():
    with _session() as db:
        _seed(db)
        visual = ai_providers.save_provider(db, provider_id=None, name="Vision", api_base="https://vision.example/v1",
            api_key="vision-secret", enabled=True, manual_models="visual")
        chat_id = ai_dispatch.get_ai_setting(db).provider_id
        setting = ai_dispatch.save_ai_setting(
            db, enabled=True, provider_id=chat_id, model="chat",
            vision_enabled=True, vision_provider_id=visual.id, vision_model="visual",
        )
        from app.services.ai_vision import snapshot_setting

        config = snapshot_setting(setting)
        assert (config.model, config.api_key) == ("visual", "vision-secret")
        assert "vision-secret" not in visual.api_key_encrypted
        ai_dispatch.save_ai_setting(
            db, enabled=True, provider_id=chat_id, model="chat",
            vision_enabled=True, vision_provider_id=visual.id, vision_model="visual",
        )
        assert snapshot_setting(setting).api_key == "vision-secret"
        ai_dispatch.save_ai_setting(
            db, enabled=True, provider_id=chat_id, model="chat",
            vision_enabled=False, vision_provider_id=None, vision_model="",
        )
        assert setting.vision_provider_id is None
        assert snapshot_setting(setting) is None
        assert ai_dispatch.snapshot_setting(setting).api_key == "k"


def test_generation_sends_actual_images_not_file_paths():
    from app.services.plugin_runtime.text_generation import GenerationImage

    captured = {}

    class Http:
        def post_json(self, url, body, **kwargs):
            captured.update(body)
            return PluginHttpResponse(200, {}, b'{"choices":[{"message":{"content":"red"}}]}', url)

    data = image_bytes()
    client = PluginTextGenerationClient(api_base="https://vision.example/v1", model="visual", http_client=Http())
    client.generate(user_prompt="这是什么颜色？", images=(GenerationImage(data, "image/png", "0.00 秒"),))
    content = captured["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "这是什么颜色？"}
    assert content[1] == {"type": "text", "text": "0.00 秒"}
    assert content[2]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(data).decode()


@pytest.mark.parametrize("media_type", [3, 43])
def test_quotes_keep_exact_native_media_ids(media_type):
    reference = "18446744073709551610"
    payload = parse_quote_payload(
        f"<msg><appmsg><title>解释一下</title><type>57</type><refermsg><type>{media_type}</type>"
        f"<svrid>{reference}</svrid><content>媒体</content></refermsg></appmsg></msg>"
    )
    assert payload["ref_external_id"] == reference


def test_image_preparation_validates_format_pixels_and_preserves_input():
    from app.services import ai_vision

    picture = ai_vision.normalize_image(image_bytes(size=(2000, 1000)))
    assert picture.mime_type == "image/jpeg"
    with Image.open(io.BytesIO(picture.data)) as result:
        assert result.size == (1536, 768)
        assert not result.getexif()
    with pytest.raises(ai_vision.VisionError, match="无法解码"):
        ai_vision.normalize_image(b"<html>not an image</html>")
    with pytest.raises(ai_vision.VisionError, match="16 MiB"):
        ai_vision.normalize_image(b"x" * (ai_vision.MAX_IMAGE_BYTES + 1))


def test_image_preparation_does_not_silently_use_first_gif_frame():
    from app.services import ai_vision

    output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, format="GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")])
    with pytest.raises(ai_vision.VisionError, match="动态图"):
        ai_vision.normalize_image(output.getvalue())


def test_video_uses_bounded_timestamped_frames_without_network(monkeypatch):
    from pathlib import Path
    from app.services import ai_vision

    calls = []
    paths = []

    def run(command, *, timeout):
        calls.append((command, timeout))
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-enable_drefs") + 1] == "0"
        if command[0] == "ffprobe":
            paths.append(Path(command[-1]))
            return b'{"streams":[{"width":1920,"height":1080}],"format":{"duration":"80"}}'
        assert command[command.index("-frames:v") + 1] == "1"
        assert "-an" in command and "-nostdin" in command
        return image_bytes()

    monkeypatch.setattr(ai_vision, "_run_media_tool", run)
    images, note = ai_vision.sample_video(b"\x00\x00\x00\x20ftypisom" + b"fake-mp4")
    assert len(images) == 8 and len(calls) == 9
    assert images[0].caption == "视频时间 5.00 秒"
    assert images[-1].caption == "视频时间 75.00 秒"
    assert "未分析声音" in note
    assert not paths[0].exists()


@pytest.mark.parametrize("duration", ["121", "nan", "inf", "0"])
def test_video_rejects_invalid_or_oversized_duration_before_decoding(monkeypatch, duration):
    from app.services import ai_vision

    calls = []
    def run(command, *, timeout):
        calls.append(command)
        return json.dumps({"streams": [{"width": 640, "height": 480}], "format": {"duration": duration}}).encode()
    monkeypatch.setattr(ai_vision, "_run_media_tool", run)
    with pytest.raises(ai_vision.VisionError, match="120 秒"):
        ai_vision.sample_video(b"\x00\x00\x00\x20ftypisom")
    assert len(calls) == 1


def test_visual_connection_test_sends_a_synthetic_image(monkeypatch):
    from types import SimpleNamespace
    from app.services import ai_vision

    captured = {}
    monkeypatch.setattr(PluginTextGenerationClient, "generate", lambda self, **kwargs: (captured.update(kwargs), SimpleNamespace(text="红色"))[1])
    assert ai_vision.test_connection(ai_vision.VisionConfig("https://models.example/v1", "vision", "key"))[0]
    assert len(captured["images"]) == 1


def test_native_ipad_video_quote_keeps_reference_id():
    from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter

    quote = WechatIpadAdapter()._parse_quote_payload(
        "<msg><appmsg><type>57</type><title>总结</title><refermsg><type>43</type><svrid>18446744073709551610</svrid></refermsg></appmsg></msg>"
    )
    assert quote["ref_external_id"] == "18446744073709551610"

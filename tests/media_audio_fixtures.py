"""合成可重复的短音调；测试只编码内存数据，不读取群友语音或连接设备。"""

from io import BytesIO
import math
import struct

import pysilk


def tone_pcm(sample_rate: int = 24000) -> bytes:
    return b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 440 * sample / sample_rate)))
                    for sample in range(sample_rate))


def silk_bytes(*, tencent: bool = True, sample_rate: int = 24000, packet_ms: int = 20) -> bytes:
    encoded = BytesIO()
    if packet_ms == 20:
        pysilk.encode(BytesIO(tone_pcm(sample_rate)), encoded, sample_rate, 24000, tencent=tencent)
    else:
        # 公共编码函数固定每包 20 ms；用同一 SDK 生成标准允许的多帧包作回归。
        from pysilk.backends.cffi._silk import ffi, lib

        state_size = ffi.new("int32_t *")
        assert lib.SKP_Silk_SDK_Get_Encoder_Size(state_size) == 0
        state = ffi.new("uint8_t[]", state_size[0])
        control = ffi.new("SKP_SILK_SDK_EncControlStruct *")
        assert lib.SKP_Silk_SDK_InitEncoder(state, control) == 0
        control.API_sampleRate = sample_rate
        control.maxInternalSampleRate = min(sample_rate, 24000)
        control.packetSize = sample_rate * packet_ms // 1000
        control.bitRate = 24000
        control.complexity = 2
        output = ffi.new("uint8_t[1250]")
        output_size = ffi.new("int16_t *")
        encoded.write(b"\x02#!SILK_V3" if tencent else b"#!SILK_V3")
        content = tone_pcm(sample_rate)
        frame_bytes = sample_rate * 2 // 50
        for offset in range(0, len(content), frame_bytes):
            chunk = ffi.from_buffer("int16_t[]", content[offset:offset + frame_bytes])
            output_size[0] = 1250
            assert lib.SKP_Silk_SDK_Encode(state, control, chunk, frame_bytes // 2, output, output_size) == 0
            if output_size[0]:
                encoded.write(struct.pack("<h", output_size[0]))
                encoded.write(bytes(ffi.buffer(output, output_size[0])))
    # 标准容器的结束标记不属于音频包，腾讯容器通常直接以最后一个包结束。
    return encoded.getvalue() + (b"" if tencent else b"\xff\xff")

"""SILK 试听子进程入口；不加载应用配置、数据库或网络，原件始终只读。"""

from io import BytesIO
from pathlib import Path
import sys
import wave

SAMPLE_RATE = 24000
MAX_SECONDS = 600
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS
PROCESS_TIMEOUT = 20


def _limit_process() -> None:
    """限制 Linux 原生解码进程，避免坏包耗尽内存、磁盘或留下含语音的 core。"""

    if sys.platform != "linux":
        return
    import resource

    for resource_id, limit in ((resource.RLIMIT_AS, 256 * 1024 * 1024),
                               (resource.RLIMIT_CPU, PROCESS_TIMEOUT),
                               (resource.RLIMIT_FSIZE, MAX_INPUT_BYTES),
                               (resource.RLIMIT_CORE, 0)):
        current = resource.getrlimit(resource_id)
        bound = min([limit, *(value for value in current if value != resource.RLIM_INFINITY)])
        resource.setrlimit(resource_id, (bound, bound))


def _read_packets(path: Path) -> list[bytes]:
    """先校验包边界再交给 C 扩展；标准结束标记只从内存副本移除。"""

    with path.open("rb") as source:
        content = source.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("SILK input is too large")
    if content.startswith(b"\x02#!SILK_V3"):
        offset = 10
    elif content.startswith(b"#!SILK_V3"):
        offset = 9
    else:
        raise ValueError("Invalid SILK header")
    packets = []
    while offset < len(content):
        if offset + 2 > len(content):
            raise ValueError("Truncated SILK packet header")
        size = int.from_bytes(content[offset:offset + 2], "little", signed=True)
        offset += 2
        if size == -1 and offset == len(content) and packets:
            return packets
        if size < 0 or size > 1250 or offset + size > len(content):
            raise ValueError("Invalid SILK packet size")
        packets.append(content[offset:offset + size])
        offset += size
        if len(packets) > MAX_SECONDS * 50:
            raise OverflowError("SILK duration exceeds preview limit")
    if not packets:
        raise ValueError("Empty SILK audio")
    return packets


class _BoundedPCM(BytesIO):
    """解码逐块写入，超过时长即终止，不先生成无限大的 PCM 临时文件。"""

    def write(self, content: bytes) -> int:
        if self.tell() + len(content) > MAX_PCM_BYTES:
            raise OverflowError("Decoded audio exceeds preview limit")
        return super().write(content)


def convert(source: Path, target: Path) -> None:
    """固定版本的 SDK 绑定逐帧解码；公共便捷接口会漏掉多帧包的后续帧。"""

    from pysilk.backends.cffi._silk import ffi, lib

    packets = _read_packets(source)
    state_size = ffi.new("int32_t *")
    if lib.SKP_Silk_SDK_Get_Decoder_Size(state_size) != 0:
        raise ValueError("SILK decoder initialization failed")
    state = ffi.new("uint8_t[]", state_size[0])
    if lib.SKP_Silk_SDK_InitDecoder(state) != 0:
        raise ValueError("SILK decoder initialization failed")
    control = ffi.new("SKP_SILK_SDK_DecControlStruct *", {"API_sampleRate": SAMPLE_RATE})
    samples = ffi.new("int16_t[]", SAMPLE_RATE // 50)
    sample_count = ffi.new("int16_t *")
    with _BoundedPCM() as pcm:
        for packet in packets:
            chunk = ffi.from_buffer("uint8_t[]", packet)
            # SILK 每包最多五个 20 ms 帧，必须读完内部帧才能消费下一个包。
            for _frame in range(5):
                sample_count[0] = SAMPLE_RATE // 50
                result = lib.SKP_Silk_SDK_Decode(state, control, 0, chunk, len(packet), samples, sample_count)
                if result != 0 or not 0 < sample_count[0] <= SAMPLE_RATE // 50:
                    raise ValueError("Invalid SILK audio frame")
                pcm.write(bytes(ffi.buffer(samples, sample_count[0] * 2)))
                if not control.moreInternalDecoderFrames:
                    break
            else:
                raise ValueError("Invalid SILK frame count")
        if not pcm.tell() or pcm.tell() % 2:
            raise ValueError("Invalid decoded audio")
        with wave.open(str(target), "wb") as sound:
            sound.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            sound.writeframes(pcm.getvalue())


def main() -> int:
    """仅返回固定错误码；不把本地路径、语音字节或扩展异常传给浏览器。"""

    try:
        _limit_process()
        convert(Path(sys.argv[1]), Path(sys.argv[2]))
    except ImportError:
        return 2
    except OverflowError:
        return 3
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

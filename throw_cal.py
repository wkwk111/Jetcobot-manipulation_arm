import sys
import json
import time
import math
from datetime import datetime


# =========================================================
# 던지기 동작 분석 알고리즘 (로봇 없이도 동작)
# =========================================================
def _ang_dist(a, b):
    """두 관절각 벡터 사이의 유클리드 거리(deg)."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def speed_profile(samples, smooth=True):
    """프레임별 각속도 크기(deg/s)를 계산한다.

    실제 기록된 t 값으로 dt를 계산하므로 샘플 간격이 일정하지 않아도 정확하다.
    smooth=True 면 3점 이동평균으로 센서 노이즈를 완화한다.
    """
    n = len(samples)
    raw = [0.0] * n
    for i in range(1, n):
        dt = samples[i]["t"] - samples[i - 1]["t"]
        if dt > 0:
            raw[i] = _ang_dist(samples[i]["angles"], samples[i - 1]["angles"]) / dt

    if smooth and n >= 3:
        spd = [raw[0]]
        spd += [(raw[i - 1] + raw[i] + raw[i + 1]) / 3 for i in range(1, n - 1)]
        spd += [raw[-1]]
        return spd
    return raw


def extract_throw(samples, speed_ratio=0.07, speed_floor=12.0, smooth=True):
    """기록된 샘플에서 던지기 동작의 주요 지점을 검출한다.

    원리: 던지기 동작은 속도 프로파일이 '정지 -> 백스윙 -> 장전 정점 ->
    전방 투척 -> 정지' 형태로 나타난다. 임계값을 넘는 구간을 동작 구간으로
    보고 앞뒤 정지 프레임을 잘라낸 뒤, 두 봉우리 사이의 속도 골짜기를
    '장전 정점(apex)'으로 검출한다.

    던지기(throw)의 정의:
        - throw_start = apex (장전된 자세). 여기서 전방 투척이 시작된다.
                        백스윙(rest -> apex)은 단순 위치잡기라 경로에서 제외.
        - throw_end   = 전방 투척이 끝나고 정착한 자세.
        - release     = 최대 속도 지점 (= 그리퍼를 여는 순간).

    반환:
        rest_idx       : 백스윙 직전의 완전 정지 자세 (위치잡기 참고용)
        apex_idx       : 장전 정점 = 던지기 시작점(throw_start)
        end_idx        : 마지막으로 움직인 자세 = 던지기 끝점(throw_end)
        release_idx    : 최대 속도 지점 (그리퍼 open 타이밍)
        threshold/peak : 사용된 임계값과 최대 속도
        speed          : 프레임별 속도 배열

    apex 검출 실패(백스윙이 없는 경우)에는 apex_idx = rest_idx 로 둔다.
    """
    n = len(samples)
    if n < 2:
        return None

    spd = speed_profile(samples, smooth=smooth)
    peak = max(spd)
    release_idx = spd.index(peak)

    # 임계값 = max(절대 하한, 최대속도의 일정 비율)
    threshold = max(speed_floor, speed_ratio * peak)
    moving = [i for i, v in enumerate(spd) if v > threshold]

    if not moving:
        # 의미 있는 움직임이 없으면 전체를 그대로 사용
        rest_idx, end_idx = 0, n - 1
    else:
        first_move, last_move = moving[0], moving[-1]
        rest_idx = max(0, first_move - 1)  # 백스윙 직전 정지 자세
        end_idx = last_move

    # 장전 정점(apex) = rest와 release 사이의 속도 골짜기
    # (백스윙이 끝나고 전방 투척이 시작되기 직전, 팔이 가장 뒤로 장전된 순간)
    # 던지기의 실제 시작점으로 사용한다.
    apex_idx = rest_idx  # 백스윙이 없으면 정지 자세가 곧 시작점
    if release_idx - rest_idx >= 3:
        apex_idx = min(range(rest_idx + 1, release_idx), key=lambda i: spd[i])

    return {
        "rest_idx": rest_idx,
        "end_idx": end_idx,
        "release_idx": release_idx,
        "apex_idx": apex_idx,
        "threshold": threshold,
        "peak": peak,
        "speed": spd,
    }


def _point(label, idx, samples, spd):
    s = samples[idx]
    return {
        "label": label,
        "index": s["index"],
        "t": s["t"],
        "speed_deg_s": round(spd[idx], 2),
        "angles": s["angles"],
    }


def build_throw_data(samples, base_info=None):
    """샘플 리스트를 받아 '시작점(apex)/끝점 -> 경로' 형식의 결과 dict를 만든다.

    던지기 = 장전 정점(apex) -> 정착(end) 구간의 전방 스윙.
    백스윙(rest -> apex)은 단순 위치잡기이므로 경로에서 제외한다.
    """
    r = extract_throw(samples)
    if r is None:
        raise ValueError("샘플이 부족하여 던지기 동작을 분석할 수 없습니다.")

    spd = r["speed"]
    si, ei = r["apex_idx"], r["end_idx"]  # 던지기 시작 = apex, 끝 = end

    # 시작점(apex) ~ 끝점 사이의 경로 지점들
    path = []
    for seq, i in enumerate(range(si, ei + 1)):
        s = samples[i]
        path.append({
            "seq": seq,
            "index": s["index"],
            "t": s["t"],
            "angles": s["angles"],
        })

    data = {}
    if base_info:
        data.update(base_info)

    data["meta"] = {
        "threshold_deg_s": round(r["threshold"], 2),
        "peak_speed_deg_s": round(r["peak"], 2),
        "path_point_count": len(path),
    }
    # 요청대로 시작점 / 끝점을 맨 앞에, 그 다음에 경로 지점들을 저장.
    # 키 이름을 이전 상수(THROW_START_ANGLES / THROW_END_ANGLES)와 맞춤.
    data["throw_start"] = _point("throw_start(apex)", si, samples, spd)
    data["throw_end"] = _point("throw_end", ei, samples, spd)
    data["release_point"] = _point("release", r["release_idx"], samples, spd)
    data["rest_pose"] = _point("rest", r["rest_idx"], samples, spd)  # 위치잡기 참고용
    # 바로 복붙 가능한 상수 형태
    data["THROW_START_ANGLES"] = samples[si]["angles"]
    data["THROW_END_ANGLES"] = samples[ei]["angles"]
    data["path"] = path
    data["raw_samples"] = samples  # 재분석을 위한 원본 보존 (필요 없으면 삭제 가능)
    return data


def reprocess(json_path, out_path=None):
    """이미 기록된 JSON 파일을 다시 분석해서 새 형식으로 저장 (로봇 불필요)."""
    with open(json_path, "r", encoding="utf-8") as f:
        src = json.load(f)
    samples = src["samples"]
    data = build_throw_data(samples, base_info={
        "source": json_path,
        "recorded_at": src.get("recorded_at"),
    })
    out_path = out_path or json_path.replace(".json", "_extracted.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _print_summary(data)
    print(f"\n저장 완료: {out_path}")
    return data


def _print_summary(data):
    print("\n[던지기 동작 분석 결과]")
    for key in ["rest_pose", "throw_start", "release_point", "throw_end"]:
        p = data.get(key)
        if p:
            print(f"  {key:14s} idx={p['index']:3d} t={p['t']:.2f}s "
                  f"v={p['speed_deg_s']:6.1f} {p['angles']}")
    print(f"  path: {len(data['path'])} waypoints (apex -> end)")
    print("\n  # 아래 두 줄을 기존 상수 자리에 그대로 붙여넣으면 됩니다")
    print(f"  THROW_START_ANGLES = {data['THROW_START_ANGLES']}")
    print(f"  THROW_END_ANGLES   = {data['THROW_END_ANGLES']}")


# =========================================================
# 로봇 기록 모드
# =========================================================
PORT = "/dev/ttyJETCOBOT"
BAUDRATE = 1000000

RECORD_DURATION = 5
SAMPLE_INTERVAL = 0.1
OUTPUT_FILE = "throw_motion_extracted.json"

SAMPLE_COUNT = int(RECORD_DURATION / SAMPLE_INTERVAL) + 1


def valid_angles(angles):
    return (
        isinstance(angles, list)
        and len(angles) == 6
        and all(isinstance(angle, (int, float)) for angle in angles)
    )


def record_and_extract():
    from pymycobot.mycobot280 import MyCobot280

    mc = MyCobot280(PORT, BAUDRATE)
    mc.thread_lock = True
    print("로봇이 연결되었습니다.")

    target_coords = [147.4, 52.6, 241.7, -177.68, 5.26, -94.11]
    print(f"목표 좌표로 이동합니다: {target_coords}")
    mc.send_coords(target_coords, 50, 0)
    time.sleep(2)

    mc.set_free_mode(0)
    result = mc.release_all_servos(1)
    print(f"서보 토크 해제 결과: {result}")
    print("로봇팔 관절이 풀렸습니다. 손으로 자유롭게 움직일 수 있습니다.")
    time.sleep(0.2)

    recorded_samples = []
    initial_angles = None

    try:
        input("\n팔을 시작 자세로 잡은 뒤 Enter를 누르세요: ")

        start_time = time.monotonic()
        angles = mc.get_angles()

        if valid_angles(angles):
            initial_angles = [round(float(a), 2) for a in angles]
            recorded_samples.append({"index": 0, "t": 0.0, "angles": initial_angles})
            print("\n[기록 시작]")
            print(f"[00] 0.000s | 초기값 | {initial_angles}")
            print("지금부터 약 3초 동안 던지는 동작을 손으로 수행하세요.")
        else:
            print(f"\n초기 각도 읽기 실패: {angles}")
            print("기록을 계속 진행합니다.")

        for index in range(1, SAMPLE_COUNT):
            target_time = start_time + index * SAMPLE_INTERVAL
            remain = target_time - time.monotonic()
            if remain > 0:
                time.sleep(remain)

            angles = mc.get_angles()
            elapsed = time.monotonic() - start_time

            if valid_angles(angles):
                sample = {
                    "index": index,
                    "t": round(elapsed, 4),
                    "angles": [round(float(a), 2) for a in angles],
                }
                recorded_samples.append(sample)
                print(f"[{index:02d}] {sample['t']:.3f}s | {sample['angles']}")
            else:
                print(f"[{index:02d}] 각도 읽기 실패: {angles}")

        print("\n기록 완료")

    except KeyboardInterrupt:
        print("\n사용자가 기록을 중단했습니다.")

    finally:
        final_angles = mc.get_angles()
        mc.focus_all_servos()
        print("서보 토크 활성화 완료")

        base_info = {
            "recorded_at": datetime.now().isoformat(),
            "record_duration_sec": RECORD_DURATION,
            "sample_interval_sec": SAMPLE_INTERVAL,
            "raw_sample_count": len(recorded_samples),
            "initial_angles": initial_angles,
        }

        # 시작점/끝점 검출 후 새 형식으로 저장. 분석 실패 시 원본만 저장.
        try:
            output_data = build_throw_data(recorded_samples, base_info=base_info)
            _print_summary(output_data)
        except Exception as e:
            print(f"\n동작 분석 실패({e}). 원본 샘플만 저장합니다.")
            output_data = dict(base_info)
            output_data["samples"] = recorded_samples

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        print(f"\n저장 완료: {OUTPUT_FILE}")
        print(f"정상 기록된 샘플 수: {len(recorded_samples)}")
        if valid_angles(final_angles):
            print("기록 종료 시점 각도:", final_angles)


# =========================================================
# 진입점
#   - 인자로 JSON 경로를 주면: 로봇 없이 기존 파일 재분석
#   - 인자가 없으면: 로봇에 연결해 새로 기록 + 분석
# =========================================================
if __name__ == "__main__":
    if len(sys.argv) > 1:
        out = sys.argv[2] if len(sys.argv) > 2 else None
        reprocess(sys.argv[1], out)
    else:
        record_and_extract()
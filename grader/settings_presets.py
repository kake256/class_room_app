"""実運用で確定した採点基準をもとにした既定プリセット。

`rubric.py`の各RUBRIC(実際に人間確定点と突き合わせて調整してきたもの)と、
ソニー特別講義課題で教員が確認済みにした`settings.json`の内容を、Web UI/MCPから
そのまま適用できる形へ整理したもの。

用途:
  - 課題ごとに一から採点基準を書かなくても、課題タイプを選ぶだけで妥当な
    初期値を設定できるようにする。
  - 適用後は課題ごとに個別調整できる(プリセットは初期値であって固定値ではない)。

満点はプリセット側で固定せず、適用時に課題のmaxPointsへ比例換算する。
ただし`score_mapping`の非線形性(例: 0点→0、1点→8、2点→9、3点→10)は
教員の意図なので、比率を保ったまま換算する。
"""
from __future__ import annotations

from typing import Any

# 内部0〜3を実点へ写す比率。ソニー課題で教員が確認済みにした配分
# (0/8/9/10 = 0%/80%/90%/100%)を既定とする。「普通に書けていれば8割」という
# 意図を保つため単純な線形(0/33/67/100%)にはしない。
KANSOU_RATIO = {"0": 0.0, "1": 0.8, "2": 0.9, "3": 1.0}
# 演習・実験系は達成段階の差を点数へ反映させる。
EXPERIMENT_RATIO = {"0": 0.0, "1": 0.6, "2": 0.8, "3": 1.0}

PRESETS: dict[str, dict[str, Any]] = {
    "kansou": {
        "label": "感想（感想文・まとめ・特別講義）",
        "description": "講義の感想・まとめ・質問レポート。ソニー特別講義で確認済みの配分を使う。",
        "rubric_key": "KANSOU",
        "ratio": KANSOU_RATIO,
        "notes": (
            "講義の感想・まとめ課題。内部評価0〜3段階で採点し、課題の満点へ換算する。"
            "答案同士を比較せず、各答案を固定基準で独立に評価する。"
            "取り組み姿勢が見える答案は好意的に扱うが、分量だけでは加点しない。"
        ),
        "levels": {
            "0": "未提出、内容を確認できない、または課題と無関係",
            "1": "講義について通常の感想が書かれている",
            "2": "講義内容への具体的な言及と、自分の考えが書かれている",
            "3": "具体的な内容を踏まえ、深い考察や自身の経験・将来との関連が書かれている",
        },
    },
    "research": {
        "label": "調査系（社会実装事例などの調査）",
        "description": "実在する製品・サービスを調べ、技術と情報源を示す課題。",
        "rubric_key": "RESEARCH",
        "ratio": EXPERIMENT_RATIO,
        "notes": (
            "社会実装事例の調査課題。事例の具体性、技術説明と情報源、考察の3点で評価する。"
            "分野の一般論だけの記述は具体性を満たさない。"
        ),
        "levels": {
            "0": "課題に沿った調査記述がない",
            "1": "分野の一般論のみ、または技術説明と情報源がともに不足",
            "2": "実在する事例を特定し、技術説明または情報源のいずれかを示している",
            "3": "具体的な事例、技術説明と情報源、社会実装への考察がそろっている",
        },
    },
    "experiment": {
        "label": "演習系（実験・演習レポート）",
        "description": "実験・演習レポート全般。距離計算やk-NNなど個別要求は課題ごとに調整する。",
        "rubric_key": "EXPERIMENT",
        "ratio": EXPERIMENT_RATIO,
        "notes": (
            "演習・実験レポート。実験結果の提示がなければ0点。"
            "定量的評価(複数条件の比較と最良条件)、実験方法と定性的評価、考察の3点で評価する。"
            "図表中で最良条件が一意に読み取れる場合は本文での再宣言を求めない。"
            "課題固有の要求(距離計算のテスト点識別、k-NNのkd-tree調査など)は"
            "適用後に課題ごとへ追記して調整する。"
        ),
        "levels": {
            "0": "実験結果(グラフまたは識別率の数値)の提示がない",
            "1": "結果の提示はあるが、比較・説明・考察のいずれも不十分",
            "2": "複数条件の比較と変更内容の説明ができている",
            "3": "最良条件の特定、方法の説明、考察がそろっている",
        },
    },
}

# 課題キー(config.yamlのassignments)からプリセットを推定するための対応。
# 一致しない課題では推定せず、利用者がプリセットを選ぶ。
ASSIGNMENT_KEY_TO_PRESET = {
    # 感想・まとめ系
    "kansou1": "kansou", "tokubetsu0511": "kansou", "ai_kansou": "kansou",
    "sukina_kansou": "kansou", "mlp_kansou": "kansou",
    # 調査系
    "application_research": "research",
    # 演習・実験系(距離計算・k-NNの専用要求は適用後に個別調整する)
    "rf": "experiment", "svm": "experiment", "adaboost": "experiment",
    "sukina": "experiment", "face_detection": "experiment", "mlp": "experiment",
    "distance": "experiment", "knn": "experiment",
}


def preset_ids() -> list[str]:
    return list(PRESETS)


def preset_for_assignment_key(assignment_key: str | None) -> str | None:
    """課題キーから既定プリセットを推定する(不明ならNone)。"""
    if not assignment_key:
        return None
    return ASSIGNMENT_KEY_TO_PRESET.get(str(assignment_key))


def build_settings(preset_id: str, max_points: float) -> dict[str, Any]:
    """プリセットを課題の満点に合わせた採点基準設定へ展開する。

    `confirmed`は付けない。教員がWeb UIで内容を確認して保存した時点で
    確認済みになる(自動で確認済みにはしない)。
    """
    preset = PRESETS.get(preset_id)
    if preset is None:
        raise ValueError("preset_idが不正です")
    points = float(max_points)
    if not points > 0:
        raise ValueError("満点は正の数で指定してください")
    mapping = {key: round(points * ratio, 2) for key, ratio in preset["ratio"].items()}
    return {
        "notes": preset["notes"],
        "levels": dict(preset["levels"]),
        "score_mapping": mapping,
        "late_penalty": 0.0,
        "confirmed": False,
        "preset_id": preset_id,
        "preset_label": preset["label"],
    }


def catalog() -> list[dict[str, Any]]:
    """UI表示用のプリセット一覧(採点基準本文は含めない)。"""
    return [{"id": key, "label": value["label"], "description": value["description"],
             "rubric_key": value["rubric_key"]} for key, value in PRESETS.items()]


__all__ = ["PRESETS", "build_settings", "catalog", "preset_for_assignment_key", "preset_ids"]

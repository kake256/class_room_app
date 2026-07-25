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
    "kansou_lecture": {
        "label": "感想文・特別講義(ソニー特別講義の確認済み基準)",
        "description": "講義の感想・まとめ課題。実運用で教員が確認済みにした基準。",
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
    "kansou_summary": {
        "label": "まとめ・質問レポート(MLP課題の基準)",
        "description": "講義内容のまとめや質問をまとめる課題。実験レポート形式ではない。",
        "rubric_key": "KANSOU",
        "ratio": KANSOU_RATIO,
        "notes": (
            "講義内容のまとめ・質問レポート。実験レポートの形式は要求しない。"
            "具体的なトピック・技術名への言及、理解の正確さ、自分の考えの3点で評価する。"
            "簡略でも本質的に正しい説明があれば0点にはしない。"
        ),
        "levels": {
            "0": "まとめ・質問の記述がない、または課題と無関係",
            "1": "まとめはあるが抽象的な言及にとどまる",
            "2": "具体的なトピックに言及し、内容を自分の言葉で整理できている",
            "3": "正確な理解に加え、自分の考察・疑問・関連づけが書かれている",
        },
    },
    "research": {
        "label": "調査課題(社会実装事例の調査)",
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
        "label": "演習・実験レポート(汎用)",
        "description": "パラメータを変えて識別率や境界を比較する演習課題。",
        "rubric_key": "EXPERIMENT",
        "ratio": EXPERIMENT_RATIO,
        "notes": (
            "演習・実験レポート。実験結果の提示がなければ0点。"
            "定量的評価(複数条件の比較と最良条件)、実験方法と定性的評価、考察の3点で評価する。"
            "図表中で最良条件が一意に読み取れる場合は本文での再宣言を求めない。"
        ),
        "levels": {
            "0": "実験結果(グラフまたは識別率の数値)の提示がない",
            "1": "結果の提示はあるが、比較・説明・考察のいずれも不十分",
            "2": "複数条件の比較と変更内容の説明ができている",
            "3": "最良条件の特定、方法の説明、考察がそろっている",
        },
    },
    "distance": {
        "label": "距離計算(専用基準)",
        "description": "テスト点の識別と距離尺度の比較。識別率一覧は要求しない。",
        "rubric_key": "DISTANCE",
        "ratio": EXPERIMENT_RATIO,
        "notes": (
            "距離計算による識別の課題。テスト点の識別結果、プログラム変更の説明、"
            "ユークリッド距離とマハラノビス距離の比較考察で評価する。"
            "この課題には識別率の一覧や最良パラメータは存在しないため、その欠如で減点しない。"
        ),
        "levels": {
            "0": "距離計算による識別への取り組みの記述がない",
            "1": "識別結果の提示のみで根拠・説明が不足",
            "2": "識別結果を根拠とともに示し、変更内容も説明できている",
            "3": "両距離の識別結果、変更説明、距離尺度の比較考察がそろっている",
        },
    },
    "knn": {
        "label": "k-NN(専用基準)",
        "description": "kの変化による境界観察とkd-tree調査。識別率一覧は要求しない。",
        "rubric_key": "KNN",
        "ratio": EXPERIMENT_RATIO,
        "notes": (
            "k-NN実習の課題。kを変えた境界変化の観察、変更箇所とkの解釈、"
            "kd-tree法の調査で評価する。"
            "識別率の数値一覧や最良のkの特定はこの課題では要求されていないため、"
            "その欠如で減点しない。"
        ),
        "levels": {
            "0": "k-NN実習への取り組みの記述がない",
            "1": "1条件のみの実行結果、または変化の記述が曖昧",
            "2": "複数のkで境界変化を観察し、変更箇所を示している",
            "3": "境界変化の観察、kの解釈、kd-tree調査がそろっている",
        },
    },
}

# 課題キー(config.yamlのassignments)からプリセットを推定するための対応。
# 一致しない課題では推定せず、利用者がプリセットを選ぶ。
ASSIGNMENT_KEY_TO_PRESET = {
    "kansou1": "kansou_lecture", "tokubetsu0511": "kansou_lecture",
    "ai_kansou": "kansou_lecture", "sukina_kansou": "kansou_summary",
    "mlp_kansou": "kansou_summary", "mlp": "experiment",
    "application_research": "research",
    "rf": "experiment", "svm": "experiment", "adaboost": "experiment",
    "sukina": "experiment", "face_detection": "experiment",
    "distance": "distance", "knn": "knn",
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

"""採点課題・ルーブリック・プロンプト・出力JSONスキーマ。

課題(courseWorkId)ごとに課題文を ASSIGNMENTS に登録する。
ルーブリック本体(3観点)はパラメータ実験系の課題で共通。"""

ASSIGNMENT = """\
RandomForest を用いて car と human の2クラスを識別する実習。
課題1: 木の数(n_estimators)と深さ(max_depth)を変更すると識別境界が
どう変化するかをグラフで確認せよ。
課題2: 識別率が最も高くなるパラメータを求めよ。
結果と考察をまとめてPDFまたはGoogleドキュメントで提出。3点満点。"""

ASSIGNMENT_SVM = """\
線形SVM を用いて car と human の2クラスを識別する実習。
課題1: パラメータ(コストC等)を変更すると識別境界(マージン)が
どう変化するかをグラフで確認せよ。
課題2: 識別率が最も高くなるパラメータを求めよ。
結果と考察をまとめてPDFまたはGoogleドキュメントで提出。3点満点。"""

ASSIGNMENT_ADABOOST = """\
AdaBoost を用いて car と human の2クラスを識別する実習。
課題1: パラメータ(弱識別器の数 n_estimators 等)を変更すると識別境界が
どう変化するかをグラフで確認せよ。
課題2: 識別率が最も高くなるパラメータを求めよ。
結果と考察をまとめてPDFまたはGoogleドキュメントで提出。3点満点。"""

ASSIGNMENT_KANSOU_1 = """\
第1回講義の内容をまとめた上で、感想を提出する課題。
今日の講義内容(機械学習の導入)をまとめ、感想を書く。
Googleドキュメントで提出。3点満点。"""

ASSIGNMENT_TOKUBETSU_0511 = """\
特別講義「AIエージェント: その概念と実装のフロンティアを探る」(5月11日)の
内容のまとめと感想を提出する課題。"""

ASSIGNMENT_SUKINA_SHUHO = """\
Regression Forest の講義動画を視聴した上で、
「ここまで学んだ機械学習の中で好きな手法とその理由」をまとめる課題。
どの手法を選んだか、その手法の内容の説明、選んだ理由(自分の考え)を
含むことが期待される。3点満点。"""

# 記号キー → (課題文, ルーブリック種別)。汎用定義のみ(courseWorkId等の
# インスタンス固有IDはここに書かない)。実際のcourseWorkId→キーの対応は
# config.yaml の assignments: に置く(gitignore対象、個人情報を公開しないため)。
ASSIGNMENT_SPECS = {
    "rf": (ASSIGNMENT, "EXPERIMENT"),
    "svm": (ASSIGNMENT_SVM, "EXPERIMENT"),
    "adaboost": (ASSIGNMENT_ADABOOST, "EXPERIMENT"),
    "kansou1": (ASSIGNMENT_KANSOU_1, "KANSOU"),
    "tokubetsu0511": (ASSIGNMENT_TOKUBETSU_0511, "KANSOU"),
    "sukina": (ASSIGNMENT_SUKINA_SHUHO, "EFFORT"),
}

RUBRIC = """\
## ゲート条件(満たさなければ合計0点)
- 実験結果の提示(識別境界のグラフ、または識別率の数値)が存在すること

## 加点観点(各1点、合計0〜3点)

1. 定量的評価と課題2への回答(1点)
   - 1点の条件(すべて必須): (a)複数条件の識別率が数値で提示・比較されている、
     (b)「最良のパラメータは n_estimators=X, max_depth=Y で識別率 Z%」のように、
     最良パラメータの値と識別率がセットで明示的に特定されている
   - (a)はあるが(b)の「値と識別率をセットで明示した特定」が読み取れない → 0.5点。
     表やグラフから読者が推測できるだけでは(b)を満たさない
   - 数値が1条件のみ、または数値なし → 0点
   - 図・表・キャプションでの提示も本文と同等に認める

2. 実験方法と定性的評価(1点)
   - 何を変更したか(パラメータやコード箇所)の説明があり、
     識別境界の変化を自分の言葉で記述している → 1点
   - 条件を系統的に変えた複数のグラフが整理されて提示されていれば、
     本文の説明が短くても達成とみなす(図リッチな答案を不利にしない)
   - グラフの羅列のみで、何を変えたかの説明が全くない → 0.5点以下

3. 考察・自分の意見(1点)
   - 1点の条件: 観察結果に対する「なぜそうなるか」のメカニズムに踏み込んだ
     理由づけ(なぜ境界が滑らかになる/複雑になるか)を、数文以上の自分の言葉で
     述べている。過学習・汎化・アンサンブル等の概念と結びつけていればなお良い
   - 観察事実の言い換え(「木を増やすと境界が複雑になった」だけ)や、
     教科書的な一般論の引き写しは 0.5点以下
   - この観点のみ文章を要求する。図の提示だけでは加点しない
   - 「面白かった」等の感想のみは 0点

{stance}

- total は観点スコアの和を四捨五入した整数(0〜3)
- 遅延(late)の減点は行わない(システム側で処理する)
- 「特に面白い・参考になる記述」は点数に含めず notable に引用として出力する"""

RUBRIC_KANSOU = """\
## ゲート条件(満たさなければ合計0点)
- 講義に関するまとめまたは感想の記述が存在すること(白紙・無関係な内容は0点)

## 加点観点(各1点、合計0〜3点)
※出力するJSONの観点名は固定( quantitative / method / discussion )だが、
  この課題では以下の意味で採点する:

1. quantitative = まとめの具体性(1点)
   - 講義で扱われた具体的なトピック・技術名・事例に言及しながら
     内容をまとめている → 1点
   - まとめはあるが「AIについて学んだ」のような抽象的な言及のみ → 0.5点
   - まとめがない → 0点

2. method = 理解の正確さと構成(1点)
   - 講義内容を自分の言葉で正しく整理・要約している → 1点
   - 講義資料の言葉の書き写し中心、または誤解がある → 0.5点

3. discussion = 感想・自分の考え(1点)
   - 具体的な感想に加え、自分の意見・疑問・今後の学習や将来との
     結びつけがある → 1点
   - 「面白かった」「勉強になった」等の一般的な感想のみ → 0.5点
   - 感想がない → 0点

{stance}

- total は観点スコアの和を四捨五入した整数(0〜3)
- 遅延(late)の減点は行わない(システム側で処理する)
- 「特に面白い・参考になる記述」は点数に含めず notable に引用として出力する"""

RUBRIC_EFFORT = """\
## 採点方針(取り組み量+概念理解の軽い確認)
この課題は「ある程度しっかり書いていて、講義で扱った概念をある程度理解して
いれば満点(3点)」とする。専門的な正確さや独自性までは求めないが、
選んだ手法の仕組みを取り違えていたり、感覚的な理由(「強そう」「なんとなく」等)
だけで概念への言及がない場合は満点にしない。

## ゲート条件(満たさなければ合計0点)
- 課題テーマに沿った記述が存在すること(白紙・一文のみ・無関係な内容は0点)

## 加点観点(各1点、合計0〜3点)
※出力するJSONの観点名は固定( quantitative / method / discussion )だが、
  この課題では以下の意味で採点する:

1. quantitative = テーマへの言及(1点)
   - 課題が求める対象(選んだ手法、講義トピック等)に具体的に言及している → 1点
   - 対象が曖昧、または触れていない → 0.5点以下

2. method = 概念理解(1点)
   - 選んだ手法・トピックの仕組みや特徴を、講義で扱った概念に沿って
     おおむね正しく説明している(平易でよい) → 1点
   - 説明が感覚的な印象だけ(「強そう」「万能感」等)で概念に触れていない、
     または明らかな誤解がある → 0.5点以下
   - 説明がほぼない(単語の列挙のみ) → 0点

3. discussion = 分量と自分の言葉(1点)
   - 数文以上のまとまった分量で、自分の言葉で理由や考えを述べている → 1点
   - 極端に短い(1〜2文程度)、明らかな手抜き → 0.5点以下

- 概念理解が示されていれば内容が平易でも3点でよい。減点は「概念への言及が
  なく感覚的」「明らかな誤解」「明らかな分量不足」のいずれかがある場合のみ
- total は観点スコアの和を四捨五入した整数(0〜3)
- 遅延(late)の減点は行わない(システム側で処理する)
- 「特に面白い・参考になる記述」は点数に含めず notable に引用として出力する"""

STANCE_STRICT = """\
## 採点姿勢(重要)
- 各観点は0点から始め、答案中に明確な根拠(引用できる記述・図)が
  ある場合のみ加点する。好意的な推測で加点しない
- 満点(合計3点)は「特に優れた答案」にのみ与える。標準的にこなした答案は2点が目安
- 部分的にしか満たさない観点は 0.5点を積極的に使う
- 判断に迷う場合は低い方のスコアを付け、あわせて flags に理由を記載する"""

STANCE_LENIENT = """\
## 採点姿勢(重要: この課題はやや甘めに採点する)
- 明らかな不足がない限り、観点は満たしているとみなして加点する(好意的に読む)
- 課題の要求に誠実に取り組んだ形跡があれば、表現の拙さでは減点しない
- 判断に迷う場合は高い方のスコアを付ける
- 0.5点は明確な欠落があるときのみ使う"""

SYSTEM_PROMPT = "あなたは大学の機械学習講義のTAで、提出レポートを厳密かつ公平に採点します。"

# rubric キー → テンプレート。EFFORT は採点方針を内蔵し {stance} を持たない
RUBRIC_TEMPLATES = {
    "EXPERIMENT": RUBRIC,
    "KANSOU": RUBRIC_KANSOU,
    "EFFORT": RUBRIC_EFFORT,
}


def resolve_assignment(
    coursework_id: str | None, assignments_map: dict[str, str] | None
) -> tuple[str, str]:
    """courseWorkId → (課題文, ルーブリック種別)。

    assignments_map は config.yaml の assignments: (courseWorkId → 記号キー)。
    coursework_id が None(calibrate等)のときは既定(rf)を使う。
    """
    if coursework_id is None:
        return ASSIGNMENT_SPECS["rf"]
    key = (assignments_map or {}).get(coursework_id)
    if key is None:
        raise KeyError(
            f"courseWorkId {coursework_id} が config.yaml の assignments: に未登録です。"
            f"assignments に '{coursework_id}: <キー>' を追加してください"
            f"(利用可能なキー: {', '.join(ASSIGNMENT_SPECS)})。"
        )
    if key not in ASSIGNMENT_SPECS:
        raise KeyError(
            f"assignments の '{coursework_id}: {key}' のキー '{key}' が未定義です"
            f"(利用可能: {', '.join(ASSIGNMENT_SPECS)})。"
        )
    return ASSIGNMENT_SPECS[key]


def build_user_prompt(
    assignment: str, rubric_key: str = "EXPERIMENT", lenient: bool | None = None
) -> str:
    rubric_template = RUBRIC_TEMPLATES[rubric_key]
    if "{stance}" in rubric_template:
        rubric = rubric_template.format(stance=STANCE_LENIENT if lenient else STANCE_STRICT)
    else:  # EFFORT等、採点方針内蔵のルーブリックは stance を差し込まない
        rubric = rubric_template
    return f"""以下のレポート課題の提出物(ページ画像)を、ルーブリックに従って採点してください。

# 課題
{assignment}

# ルーブリック
{rubric}

# 出力指示
- 指定のJSONスキーマに従い、JSONのみを出力すること
- criteria は観点1〜3を name: "quantitative" / "method" / "discussion" の順で3件出力すること
- evidence は本文の短い引用、またはページ番号+図の説明(例: "p.2 の3条件の境界比較図")とする。
  実在しない内容を書かないこと。加点する観点には必ず具体的な根拠を書くこと
- 判断に迷った場合の扱いは上記「採点姿勢」に従い、flags に理由を記載すること"""

GRADING_SCHEMA = {
    "type": "object",
    "properties": {
        "gate": {
            "type": "object",
            "properties": {
                "pass": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["pass", "reason"],
        },
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "score": {"type": "number"},
                    "evidence": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["name", "score", "evidence", "comment"],
            },
        },
        "total": {"type": "number"},
        "flags": {"type": "array", "items": {"type": "string"}},
        "notable": {"type": ["string", "null"]},
    },
    "required": ["gate", "criteria", "total", "flags", "notable"],
}

CRITERIA_NAMES = ["quantitative", "method", "discussion"]

"""조항호목 파서 — DB에 접촉하지 않는 순수 함수 모듈.

법제처 API 응답(dict)을 좌표가 붙은 노드 리스트로 바꾼다.
파서를 개선했을 때 raw/{law_id}/{version_key}.json.gz 로 재실행해
law_articles를 통째로 재생성할 수 있어야 하므로, 여기에는 부작용을 두지 않는다.

파서 규칙의 전제가 되는 법제처 응답의 성질:
  · 목은 대개 '항'의 직계로 오지만(호와 형제) 전부는 아니다. 호 안에 중첩돼
    오는 것도 있어 양쪽을 다 읽어야 한다. 항 직속은 소속 호를 추론해야
    하지만(_assign_mok), 호 중첩은 API가 부모를 알려 준 것이라 추론이 없다.
  · 목번호는 가~하 → 거~허 → 고~호 로 42자까지 순환한다
  · 호번호에 가지번호가 있다 ('7의2.' 등)
  · 행정규칙은 조문형식여부=Y면 조문내용 배열의 원소 1개 = 조문 1개
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 상수 · 정규식
# ============================================================

# 진짜 HTML 태그만 지운다. <[^>]+>로 뭉뚱그리면 '<개정 2020.12.28.>' 같은
# 개정 이력 표기까지 지워진다 (core.py와 같은 이유).
HTML_TAG_RE = re.compile(
    r"</?\s*(?:img|br|p|div|span|table|thead|tbody|tr|td|th|ul|ol|li|"
    r"a|b|i|u|em|strong|font|hr)\b[^>]*>", re.I)

# 목번호 42자 순환. 14자만 매핑하면 '거' 이후가 전부 0이 되어 좌표가 뭉갠다.
MOK_SEQ = ("가나다라마바사아자차카타파하"      # 1~14
           "거너더러머버서어저처커터퍼허"      # 15~28
           "고노도로모보소오조초코토포호")     # 29~42

# 조문 라벨 — 행정규칙 텍스트와 부칙에서 조문번호를 뽑을 때 쓴다
ART_LABEL_RE = re.compile(r"^\s*제\s*(\d+)\s*조(?:의\s*(\d+))?")

# 호에 목이 딸려 있음을 알리는 표현 ('다음 각 목의', '각 목의 어느')
MOK_ANCHOR_RE = re.compile(r"각\s*목")

# 호내용 첫머리의 '3의2.' — 호번호 필드가 가지번호를 빠뜨렸을 때 여기서 집는다
ITEM_IN_BODY_RE = re.compile(r"^\s*(\d+)\s*의\s*(\d+)\s*\.")

# 부칙 헤더 — "부칙 <제20883호,2025.4.1>" / "부칙(정부조직법) <제21065호,…>"
ADDENDA_HEAD_RE = re.compile(r"^부칙\s*(?:\(([^)]*)\))?\s*<\s*제?([^,>]*),\s*([^>]*)>")

# 부칙 시행일 조항 — 라벨이 있는 경우
EFF_CLAUSE_RE = re.compile(r"^\s*제\s*1\s*조\s*\(\s*시행일\s*\)")

# 분리시행 문자열 — "20251002:제2조제1호", 구분자 없이 다음 날짜가 이어붙는다
SPLIT_DATE_RE = re.compile(r"(?=\d{8}\s*:)")
SPLIT_PAIR_RE = re.compile(r"(\d{8})\s*:\s*(.*)", re.S)


# ============================================================
# 좌표 변환
# ============================================================

def norm_text(s: Any) -> str:
    """표시용 정규화 — HTML 태그만 걷어내고 양끝 공백 제거."""
    return HTML_TAG_RE.sub("", str(s or "")).strip()


def hash_body(s: str) -> str:
    """비교용 해시 — 공백 차이로 인한 헛diff를 막기 위해 공백을 압축한 뒤 해시."""
    return hashlib.sha256(
        re.sub(r"\s+", " ", str(s or "")).strip().encode("utf-8")).hexdigest()


def para_to_int(s: Any) -> int:
    """항번호 → 정수. '①'→1. 원문자 20을 넘는 구간도 처리한다.

    21항 이상을 0으로 떨어뜨리면 같은 조문의 항들이 좌표상 구분되지 않는다.
    """
    t = str(s or "").strip()
    if not t:
        return 0
    o = ord(t[0])
    if 0x2460 <= o <= 0x2473:          # ①~⑳
        return o - 0x2460 + 1
    if 0x3251 <= o <= 0x325F:          # ㉑~㉟
        return o - 0x3251 + 21
    if 0x32B1 <= o <= 0x32BF:          # ㊱~㊿
        return o - 0x32B1 + 36
    m = re.match(r"\(?(\d+)\)?", t)    # '(1)' / '1' 형태 폴백
    return int(m.group(1)) if m else 0


def item_to_int(s: Any) -> Tuple[int, int]:
    """호번호 → (호번호, 가지번호). '7의2.' → (7, 2), '3.' → (3, 0).

    가지번호를 버리면 제7호와 제7호의2가 같은 좌표로 뭉갠다.
    """
    m = re.match(r"\s*(\d+)(?:\s*의\s*(\d+))?", str(s or ""))
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2) or 0)


def item_no_of(ho: Dict) -> Tuple[int, int, str]:
    """호 하나의 (호번호, 가지번호, 표시라벨).

    호번호 필드만 믿으면 안 된다. 법제처는 '1의2.' 호에도 호번호를 '1.'로
    보내면서 진짜 번호는 호내용 첫머리에만 넣는 경우가 있다. 그대로 두면
    제1호와 제1호의2가 같은 좌표를 갖게 되어, 대조에서 한쪽이 다른 쪽을
    덮어쓰고 인용 대조도 엉뚱한 조문을 집는다.

    본문에서 집은 번호가 호번호 필드와 어긋나면 쓰지 않는다 — 필드가 맞고
    본문이 이상한 경우까지 뒤집을 근거는 없다.
    """
    no, br = item_to_int(ho.get("호번호", ""))
    label = str(ho.get("호번호", "")).strip()
    if not br:
        m = ITEM_IN_BODY_RE.match(norm_text(ho.get("호내용", "")))
        if m and int(m.group(1)) == no:
            br = int(m.group(2))
            label = f"{no}의{br}."
    return no, br, label


def sub_to_int(s: Any) -> int:
    """목번호 → 정수. '가.'→1, '거.'→15, '고.'→29. 42자까지."""
    t = str(s or "").strip()
    return MOK_SEQ.index(t[0]) + 1 if t and t[0] in MOK_SEQ else 0


def art_to_int(s: Any, branch: Any = "") -> Tuple[int, int]:
    """조문번호 → (조번호, 가지번호). 응답이 '4'/'2' 형태로 따로 온다."""
    def _i(v):
        m = re.match(r"\s*(\d+)", str(v or ""))
        return int(m.group(1)) if m else 0
    return _i(s), _i(branch)


def parse_split_enforce(raw: Any) -> List[Tuple[str, str]]:
    """조문시행일자문자열 → [(YYYYMMDD, 조문표기)].

    형식이 지저분하다. 구분자 없이 다음 날짜가 바로 이어붙는다:
      "20241203:제17조제5호,제17조제5호의220250304:제11조제2항…"
                                    ^^^^^^^^ 여기서 끊어야 한다
    8자리 뒤에 콜론이 오는 위치만 분할점으로 삼는다.
    파싱이 깨져도 원문(split_enforce_raw)을 따로 보존하므로 여기서는 최선만 한다.
    """
    out: List[Tuple[str, str]] = []
    txt = str(raw or "").strip()
    if not txt:
        return out
    for part in SPLIT_DATE_RE.split(txt):
        m = SPLIT_PAIR_RE.match(part.strip())
        if m and m.group(2).strip():
            out.append((m.group(1), m.group(2).strip()))
    return out


# ============================================================
# 자료구조
# ============================================================

@dataclass
class ArticleNode:
    """law_articles 한 행에 대응. seq가 무결성을 보장하고 좌표는 표시·조회용."""
    seq: int
    depth: int                  # 0전문 1조 2항 3호 4목
    art_no: int = 0
    art_branch: int = 0
    para_no: int = 0
    item_no: int = 0
    item_branch: int = 0
    sub_no: int = 0
    item_inferred: int = 0      # 1 = 목의 호 귀속을 판정하지 못해 항 직속으로 둠
    label: str = ""
    art_title: str = ""
    body: str = ""
    body_hash: str = ""
    art_eff_date: str = ""
    revise_type: str = ""
    changed_flag: str = ""

    def coord(self) -> Tuple[int, int, int, int, int, int]:
        return (self.art_no, self.art_branch, self.para_no,
                self.item_no, self.item_branch, self.sub_no)

    def cite(self) -> str:
        """사람이 읽는 조문 표기. 호 귀속을 못 정했으면 호를 생략한다.
        틀린 번호를 찍는 것보다 생략이 낫다.

        편장절 제목('제1장 총칙')은 조문번호가 없으므로 빈 문자열을 돌려준다.
        """
        s = f"제{self.art_no}조" if self.art_no else ""
        if self.art_branch:
            s += f"의{self.art_branch}"
        if self.para_no:
            s += f"제{self.para_no}항"
        if self.item_no:
            s += f"제{self.item_no}호"
            if self.item_branch:
                s += f"의{self.item_branch}"
        if self.sub_no:
            s += ("" if self.item_no else " ") + f"{MOK_SEQ[self.sub_no - 1]}목"
        return s


@dataclass
class AddendaBlock:
    """law_addenda 한 행. 부칙은 판본이 아니라 법령에 종속되고 누적된다."""
    promulgation_date: str = ""
    promulgation_no: str = ""
    header: str = ""
    source_law: str = ""        # 타법개정이면 그 법 이름 ('부칙(정부조직법)')
    body: str = ""
    effective_clause: str = ""
    has_split_enforce: int = 0
    body_hash: str = ""


@dataclass
class ParsedLaw:
    law_id: str = ""
    version_key: str = ""
    title: str = ""
    ministry: str = ""
    law_type: str = ""
    is_admrul: int = 0
    announced_date: str = ""
    enforce_date_d: str = ""            # 전문API 기본정보 시행일자
    split_enforce_raw: str = ""
    parse_status: str = "failed"        # structured / article / text / failed
    nodes: List[ArticleNode] = field(default_factory=list)
    addenda: List[AddendaBlock] = field(default_factory=list)
    splits: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def node_count(self) -> int:
        return len(self.nodes)


# ============================================================
# 내부 헬퍼
# ============================================================

def _lst(x: Any) -> List:
    """법제처 응답은 원소가 1개면 dict, 여러 개면 list로 온다."""
    if isinstance(x, dict):
        return [x]
    return x if isinstance(x, list) else ([] if x is None else [x])


def _assign_mok(hos: List[Dict], moks: List[Dict]) -> List[Tuple[Dict, int, int, int]]:
    """목을 호에 귀속시킨다. → [(목, item_no, item_branch, inferred)]

    API가 목의 소속 호를 알려주지 않아 추론해야 한다. 근거가 확실할 때만
    귀속시킨다 — '각 목'을 언급한 앵커 호가 정확히 1개일 때다. 나머지는
    틀린 호에 붙이는 대신 항 직속으로 두고 플래그를 세운다.

    inferred 의미:
      0 = 확정(앵커 1개) 또는 호가 아예 없어 진짜 항 직속
      1 = 호는 있는데 귀속을 판정하지 못함
    """
    if not moks:
        return []
    if not hos:
        # 호가 없으면 목은 항의 직계가 맞다. 추론이 아니다.
        return [(m, 0, 0, 0) for m in moks]
    # 제 목을 이미 달고 온 호는 앵커에서 뺀다 — 그 호의 목은 중첩 경로에서
    # 이미 좌표까지 붙여 만들었다. 여기서 또 앵커로 잡으면 떠 있는 목이
    # 그 호에 얹혀 좌표가 겹친다.
    anchors = [o for o in hos
               if not o.get("목")
               and MOK_ANCHOR_RE.search(str(o.get("호내용", "")))]
    if len(anchors) == 1:
        no, br, _ = item_no_of(anchors[0])
        return [(m, no, br, 0) for m in moks]
    # 앵커가 0개거나 2개 이상 → 판정 불가. 항 직속 + 플래그.
    return [(m, 0, 0, 1) for m in moks]


def _mk(seq: int, depth: int, body: str, **kw) -> ArticleNode:
    b = norm_text(body)
    return ArticleNode(seq=seq, depth=depth, body=b, body_hash=hash_body(b), **kw)


# ============================================================
# 부칙 파서
# ============================================================

def _effective_clause(lines: List[str]) -> Tuple[str, int]:
    """부칙 줄 배열에서 시행일 규정을 뽑는다. → (본문, 분리시행여부)

    긴 부칙은 '제1조(시행일)' 라벨이 있지만, 짧은 부칙은 라벨 없이
    바로 문장이 온다:
        "부칙 <제20883호,2025.4.1>"
        "이 법은 공포한 날부터 시행한다. 다만, 제2조제1호의 개정규정은…"
    라벨만 찾으면 이 케이스를 통째로 놓치므로 헤더 다음 첫 줄로 폴백한다.
    """
    body = [x for x in lines if str(x).strip()]
    if not body:
        return "", 0
    # 행정규칙 부칙은 줄바꿈 없이 헤더와 본문이 한 줄로 붙어서 온다.
    #   "부칙 <제2011-1호,2010.12.17.>이 고시는 2011. 1. 1.부터 시행한다."
    # 헤더 줄을 통째로 건너뛰면 남는 내용이 없어져 시행일 조항을 못 뽑는다.
    first = str(body[0]).strip()
    m = ADDENDA_HEAD_RE.match(first)
    rest: List[str] = []
    if m:
        tail = first[m.end():].strip()
        if tail:
            rest.append(tail)
        rest.extend(str(x) for x in body[1:])
    else:
        rest = [str(x) for x in body]
    if not rest:
        return "", 0

    picked: List[str] = []
    for i, ln in enumerate(rest):
        if EFF_CLAUSE_RE.match(str(ln)):
            picked.append(str(ln))
            # 시행일 조항에 딸린 하위 항·호(들여쓰기 또는 번호)를 함께 담는다.
            for nxt in rest[i + 1:]:
                s = str(nxt)
                if ART_LABEL_RE.match(s) and not EFF_CLAUSE_RE.match(s):
                    break
                picked.append(s)
            break
    if not picked:
        picked = [str(rest[0])]     # 라벨 없는 짧은 부칙

    txt = "\n".join(picked).strip()
    return txt, (1 if "다만" in txt else 0)


def parse_addenda_law(block: Any) -> List[AddendaBlock]:
    """법령 부칙 — 부칙단위[] 각각이 { 부칙공포일자, 부칙내용, 부칙공포번호 }.
    부칙내용은 list[1] of list(줄 배열) 형태로 온다."""
    out: List[AddendaBlock] = []
    units = _lst(block.get("부칙단위")) if isinstance(block, dict) else []
    for u in units:
        if not isinstance(u, dict):
            continue
        lines: List[str] = []
        for blk in _lst(u.get("부칙내용")):
            if isinstance(blk, list):
                lines.extend(str(x) for x in blk)
            else:
                lines.append(str(blk))
        lines = [norm_text(x) for x in lines]
        body = "\n".join(x for x in lines if x)
        if not body:
            continue
        raw_head = lines[0] if lines else ""
        m = ADDENDA_HEAD_RE.match(raw_head)
        # 헤더에 본문이 붙어 온 경우(행정규칙) 헤더 부분만 잘라 낸다
        head = raw_head[:m.end()] if m else raw_head
        eff, split = _effective_clause(lines)
        out.append(AddendaBlock(
            promulgation_date=str(u.get("부칙공포일자", "")).strip(),
            promulgation_no=str(u.get("부칙공포번호", "")).strip(),
            header=head,
            # 타법개정 부칙이면 괄호 안이 그 법 이름이다. 이 경우 부칙 본문의
            # 조문번호는 '그 법' 기준이라 대상 법령의 조문으로 읽으면 안 된다.
            source_law=(m.group(1) or "") if m else "",
            body=body, effective_clause=eff, has_split_enforce=split,
            body_hash=hash_body(body)))
    return out


def parse_addenda_admrul(block: Any) -> List[AddendaBlock]:
    """행정규칙 부칙 — 법령과 달리 병렬 배열이다.
    { 부칙공포일자:[...], 부칙내용:[...], 부칙공포번호:[...] }"""
    out: List[AddendaBlock] = []
    bc = block.get("부칙") if isinstance(block, dict) else None
    if not isinstance(bc, dict):
        return out
    bodies = _lst(bc.get("부칙내용"))
    dates = _lst(bc.get("부칙공포일자"))
    nos = _lst(bc.get("부칙공포번호"))
    for i, raw in enumerate(bodies):
        lines = [norm_text(x) for x in (raw if isinstance(raw, list) else [raw])]
        body = "\n".join(x for x in lines if x)
        if not body:
            continue
        eff, split = _effective_clause(lines)
        out.append(AddendaBlock(
            promulgation_date=str(dates[i]).strip() if i < len(dates) else "",
            promulgation_no=str(nos[i]).strip() if i < len(nos) else "",
            header=lines[0] if lines else "",
            body=body, effective_clause=eff, has_split_enforce=split,
            body_hash=hash_body(body)))
    return out


# ============================================================
# 법령 파서
# ============================================================

def _content(v: Any) -> str:
    """법제처가 {'content': '값', ...} 로 주는 필드에서 값만 꺼낸다.

    소관부처·법종구분이 이 형태다. 그냥 str()로 감싸면 dict 표기가 그대로
    DB에 들어가 화면에 찍힌다.
    """
    if isinstance(v, dict):
        return str(v.get("content", "") or "").strip()
    return str(v or "").strip()


def parse_law(detail: Dict, law_id: str = "", version_key: str = "") -> ParsedLaw:
    """현행법령 전문(target=law) 응답 → ParsedLaw."""
    d = detail.get("법령", detail) if isinstance(detail, dict) else {}
    info = d.get("기본정보", {}) if isinstance(d.get("기본정보"), dict) else {}
    mn = info.get("소관부처")

    out = ParsedLaw(
        law_id=law_id or str(info.get("법령ID", "")),
        version_key=version_key,
        title=str(info.get("법령명_한글", "")).strip(),
        ministry=(mn.get("content", "") if isinstance(mn, dict) else str(mn or "")),
        # 법종구분은 {'content': '법률', '법종구분코드': 'A0002'} 로 온다.
        # str()로 감싸면 dict 표기가 그대로 화면에 찍힌다(소관부처와 같은 형태).
        law_type=_content(info.get("법종구분")),
        announced_date=str(info.get("공포일자", "")).strip(),
        enforce_date_d=str(info.get("시행일자", "")).strip(),
        # 분리시행의 유일한 정답 소스. 파싱이 깨져도 원문은 그대로 보존한다.
        split_enforce_raw=str(info.get("조문시행일자문자열", "") or ""),
    )
    out.splits = parse_split_enforce(out.split_enforce_raw)

    arts = d.get("조문", {})
    arts = _lst(arts.get("조문단위") if isinstance(arts, dict) else arts)
    if not arts:
        out.warnings.append("조문단위 없음")
        out.parse_status = "failed"
        return out

    seq = 0
    for a in arts:
        if not isinstance(a, dict):
            continue
        art_no, art_branch = art_to_int(a.get("조문번호"), a.get("조문가지번호"))
        art_title = norm_text(a.get("조문제목"))
        eff = str(a.get("조문시행일자", "") or "")
        rev = str(a.get("조문제개정유형", "") or "")
        chg = str(a.get("조문변경여부", "") or "")
        # 편장절 제목('제1장 총칙')은 조문번호가 다음 조문 것으로 채워져 오므로
        # 조문으로 취급하면 번호가 어긋난다. depth는 주되 좌표는 비운다.
        is_heading = a.get("조문여부") == "전문"

        seq += 1
        out.nodes.append(_mk(
            seq, 1, a.get("조문내용"),
            art_no=0 if is_heading else art_no,
            art_branch=0 if is_heading else art_branch,
            label=("" if is_heading else f"제{art_no}조"
                   + (f"의{art_branch}" if art_branch else "")),
            art_title=art_title, art_eff_date=eff,
            revise_type=rev, changed_flag=chg))
        if is_heading:
            continue

        for h in _lst(a.get("항")):
            if not isinstance(h, dict):
                continue
            para_lbl = str(h.get("항번호", "") or "")
            para_no = para_to_int(para_lbl)
            ptxt = norm_text(h.get("항내용"))
            if ptxt:
                seq += 1
                out.nodes.append(_mk(
                    seq, 2, ptxt, art_no=art_no, art_branch=art_branch,
                    para_no=para_no, label=para_lbl.strip(),
                    art_title=art_title, art_eff_date=eff))

            hos = [o for o in _lst(h.get("호")) if isinstance(o, dict)]

            # 목을 먼저 다 모아 소속 호를 정한 다음, 호 뒤에 끼워 넣는다.
            #
            # 원본이 목을 두 모양으로 준다 — 항 직속으로 주기도 하고 호 안에
            # 중첩해 주기도 하며, 같은 조문이 판본마다 다른 모양으로 온다.
            #
            # 원본 모양을 그대로 흘리면 노드 순서가 판본마다 달라지고, 대조
            # (core.diff_versions)가 그것을 내용 변경으로 읽는다. 여기서
            # 순서를 한 가지로 고정해 막는다.
            #
            # 귀속이 정해진 목은 제 호 바로 뒤, 못 정한 목은 항 맨 뒤에 둔다.
            # 없는 소속을 지어내지 않으면서 순서는 안정된다.
            by_item: Dict[Tuple[int, int], List[Tuple[Dict, int]]] = {}
            loose: List[Tuple[Dict, int]] = []
            for o in hos:
                ino, ibr, _ = item_no_of(o)
                for m in _lst(o.get("목")):
                    if isinstance(m, dict):        # 호 중첩 — 부모가 명시됐다
                        by_item.setdefault((ino, ibr), []).append((m, 0))
            moks = [m for m in _lst(h.get("목")) if isinstance(m, dict)]
            for m, ino, ibr, inferred in _assign_mok(hos, moks):
                if inferred or (ino, ibr) == (0, 0):
                    loose.append((m, inferred))
                else:
                    by_item.setdefault((ino, ibr), []).append((m, inferred))

            def _mok(m, ino, ibr, inferred, _seq):
                return _mk(_seq, 4, m.get("목내용"), art_no=art_no,
                           art_branch=art_branch, para_no=para_no,
                           item_no=ino, item_branch=ibr,
                           sub_no=sub_to_int(m.get("목번호", "")),
                           item_inferred=inferred,
                           label=str(m.get("목번호", "")).strip(),
                           art_title=art_title, art_eff_date=eff)

            for o in hos:
                ino, ibr, ilabel = item_no_of(o)
                seq += 1
                out.nodes.append(_mk(
                    seq, 3, o.get("호내용"), art_no=art_no, art_branch=art_branch,
                    para_no=para_no, item_no=ino, item_branch=ibr,
                    label=ilabel,
                    art_title=art_title, art_eff_date=eff))
                # pop이라야 좌표가 같은 호가 둘 있어도 목이 두 번 붙지 않는다
                for m, inferred in by_item.pop((ino, ibr), []):
                    seq += 1
                    out.nodes.append(_mok(m, ino, ibr, inferred, seq))

            # 소속을 못 정한 목은 항 맨 뒤에 좌표 없이 둔다.
            for m, inferred in loose:
                seq += 1
                out.nodes.append(_mok(m, 0, 0, inferred, seq))
            # 어느 호에도 걸리지 않고 남은 것(같은 좌표의 호가 둘 이상일 때만
            # 생긴다). 좌표는 알고 있으므로 버리지 않고 그대로 붙인다.
            for (ino, ibr), rest in by_item.items():
                for m, inferred in rest:
                    seq += 1
                    out.nodes.append(_mok(m, ino, ibr, inferred, seq))

    out.addenda = parse_addenda_law(d.get("부칙"))
    out.parse_status = "structured" if out.nodes else "failed"
    return out


# ============================================================
# 행정규칙 파서
# ============================================================

def parse_admrul(detail: Dict, law_id: str = "", version_key: str = "") -> ParsedLaw:
    """행정규칙(target=admrul) 응답 → ParsedLaw.

    조문형식여부=Y 면 조문내용 배열의 원소 1개가 조문 1개다.
    조 단위로 law_articles에 넣으면 법령과 같은 diff 로직을 쓸 수 있다.
    N 이면 구조가 없으므로 전문 1행(depth=0)으로 폴백한다.
    """
    blk = detail.get("AdmRulService", detail) if isinstance(detail, dict) else {}
    info = blk.get("행정규칙기본정보", {}) if isinstance(blk, dict) else {}

    out = ParsedLaw(
        law_id=law_id,
        version_key=version_key,
        title=str(info.get("행정규칙명", "")).strip(),
        ministry=str(info.get("소관부처명", "") or ""),
        law_type=str(info.get("행정규칙종류", "행정규칙") or "행정규칙"),
        is_admrul=1,
        announced_date=str(info.get("발령일자", "")).strip(),
        enforce_date_d=str(info.get("시행일자", "")).strip(),
    )

    body = blk.get("조문내용")
    items = [norm_text(x) for x in _lst(body)]
    items = [x for x in items if x]
    formatted = str(info.get("조문형식여부", "")).strip().upper() == "Y"

    if formatted and items:
        for i, txt in enumerate(items, start=1):
            m = ART_LABEL_RE.match(txt)
            ano = int(m.group(1)) if m else 0
            abr = int(m.group(2) or 0) if m else 0
            if not m:
                out.warnings.append(f"조문 라벨 없음: seq={i}")
            out.nodes.append(_mk(
                i, 1, txt, art_no=ano, art_branch=abr,
                label=(f"제{ano}조" + (f"의{abr}" if abr else "")) if m else ""))
        out.parse_status = "article"
    elif items:
        # 조문 형식이 아닌 고시 — 전문 한 덩어리
        txt = "\n".join(items)
        out.nodes.append(_mk(1, 0, txt))
        out.parse_status = "text"
        out.warnings.append("조문형식여부=N — 전문 텍스트로 폴백")
    else:
        out.warnings.append("조문내용 없음")
        out.parse_status = "failed"

    out.addenda = parse_addenda_admrul(blk)
    return out


# ============================================================
# diff
# ============================================================

def render_fulltext(nodes: List[ArticleNode], title: str = "",
                    ministry: str = "") -> str:
    """law_fulltext.content용 평문 — 화면 표시·본문 검색의 안전망.

    조항호목 구조가 있어도 이 텍스트를 함께 유지한다(의도된 중복).
    파서가 잘못 잡았을 때 사람이 원문을 확인할 수단이 필요하고,
    LIKE 검색도 여기서 걸린다.
    """
    out: List[str] = []
    if title:
        out.append(f"[법령명] {title}")
    if ministry:
        out.append(f"[소관부처] {ministry}")
    if out:
        out.append("")
    for n in nodes:
        if not n.body:
            continue
        out.append("  " * max(0, n.depth - 1) + n.body)
    return "\n".join(out)[:200000]


def node_from_row(r: Dict) -> ArticleNode:
    """law_articles 행(dict) → ArticleNode. DB에서 읽은 판본을 diff에 태울 때 쓴다."""
    return ArticleNode(
        seq=int(r.get("seq") or 0), depth=int(r.get("depth") or 0),
        art_no=int(r.get("art_no") or 0), art_branch=int(r.get("art_branch") or 0),
        para_no=int(r.get("para_no") or 0), item_no=int(r.get("item_no") or 0),
        item_branch=int(r.get("item_branch") or 0), sub_no=int(r.get("sub_no") or 0),
        item_inferred=int(r.get("item_inferred") or 0),
        label=r.get("label") or "", art_title=r.get("art_title") or "",
        body=r.get("body") or "", body_hash=r.get("body_hash") or "")


def affected_articles(diff: Dict[str, List]) -> List[Tuple[int, int]]:
    """변경이 걸친 조문 좌표 목록 (조번호, 가지번호)."""
    keys = set()
    for o, n in diff.get("changed", []):
        keys.add((n.art_no, n.art_branch))
    for n in diff.get("added", []):
        keys.add((n.art_no, n.art_branch))
    for o in diff.get("removed", []):
        keys.add((o.art_no, o.art_branch))
    for o, n in diff.get("moved", []):
        keys.add((o.art_no, o.art_branch))
        keys.add((n.art_no, n.art_branch))
    # (0,0)도 남긴다 — 편·장·절 제목과 전문 폴백이 여기 걸린다. 걸러 내면
    # '변경은 있는데 본문은 빈' 개정 건이 기록된다.
    return sorted(keys)


def render_articles(nodes: List[ArticleNode], keys: List[Tuple[int, int]],
                    marked: Optional[set] = None) -> str:
    """지정한 조문만 골라 조문 전체를 들여쓰기 텍스트로 만든다.

    변경된 노드만 떼서 LLM에 넘기면 문맥을 잃는다("제5조제2항제3호"만 보면
    무슨 조문인지 알 수 없다). 그래서 해당 조문 전체와 조문제목을 함께 싣고,
    바뀐 줄만 ▶로 표시한다.

    marked는 좌표가 아니라 seq 집합이다. 좌표는 유일하지 않아서(편·장·절
    제목은 전부 (0,0,0,0,0,0)) 좌표로 표시하면 장 제목 하나가 바뀌었을 때
    모든 장·절 제목에 ▶가 붙는다. seq는 판본 안에서 유일하다.
    """
    want = set(keys)
    marked = marked or set()
    out: List[str] = []
    cur: Optional[Tuple[int, int]] = None

    def _order(n: ArticleNode):
        # API는 항 밑에 호를 전부 준 뒤 목을 몰아서 준다. 그대로 찍으면
        # "다음 각 목의…"라고 한 제1호와 목 사이에 호 2~7이 끼어 읽기 나쁘다.
        # 호가 확정된 목은 그 호 바로 뒤로 보내고, 귀속을 못 정한 목은
        # 항의 맨 뒤에 모아 둔다(없는 소속을 지어내지 않기 위해).
        item = n.item_no if (n.depth < 4 or n.item_no) else 10 ** 6
        return (n.art_no, n.art_branch, n.para_no, item,
                n.item_branch, n.sub_no, n.seq)

    for n in sorted(nodes, key=_order):
        if (n.art_no, n.art_branch) not in want:
            continue
        if (n.art_no, n.art_branch) != cur:
            cur = (n.art_no, n.art_branch)
            if n.art_no:
                head = f"제{n.art_no}조" + (f"의{n.art_branch}" if n.art_branch else "")
                if n.art_title:
                    head += f"({n.art_title})"
            else:
                # 조문번호가 없는 노드 — '제0조'로 찍으면 있지도 않은 조를
                # 만들어 낸다. 전문 폴백(depth 0)과 편·장·절 제목(depth 1)이
                # 여기 온다.
                head = "전문" if n.depth == 0 else "편ㆍ장ㆍ절 제목"
            out.append(f"\n[{head}]")
        flag = "▶ " if n.seq in marked else "  "
        indent = "  " * max(0, n.depth - 1)
        out.append(f"{flag}{indent}{n.body}")
    return "\n".join(out).strip()


def render_diff(old: List[ArticleNode], new: List[ArticleNode],
                diff: Dict[str, List]) -> Tuple[str, str, int]:
    """diff → (개정 전 텍스트, 개정 후 텍스트, 변경 노드 수).

    영향받은 조문 전체를 양쪽에서 뽑아 대조할 수 있게 만든다.
    """
    keys = affected_articles(diff)
    # seq로 표시한다 — 좌표는 유일하지 않다(render_articles 주석 참조).
    mark_old = {o.seq for o, _ in diff.get("changed", [])}
    mark_old |= {o.seq for o in diff.get("removed", [])}
    mark_new = {n.seq for _, n in diff.get("changed", [])}
    mark_new |= {n.seq for n in diff.get("added", [])}
    cnt = (len(diff.get("changed", [])) + len(diff.get("added", []))
           + len(diff.get("removed", [])) + len(diff.get("moved", [])))
    return (render_articles(old, keys, mark_old),
            render_articles(new, keys, mark_new), cnt)


def is_coord_refinement(old: ArticleNode, new: ArticleNode) -> bool:
    """좌표가 달라졌지만 '같은 조항'인가 — 호 귀속이 미정↔확정으로만 바뀐 경우.

    법제처가 목을 항 직속으로 주면 소속 호를 알 수 없어 item_no를 비워 두는데
    (_assign_mok), 같은 조문을 호 중첩으로 주면 부모가 명시돼 좌표가 채워진다.
    같은 조문이 판본마다 다른 모양으로 오므로 이것을 변경으로 세면, 법에는
    아무 일도 없었는데 조문 수십 개가 옮겨 다닌 것처럼 보인다.

    본문이 한 글자도 다르지 않고, 조·항·목번호가 같으며, 호 귀속만 한쪽이
    비어 있을 때로 한정한다. 진짜 조문 이동(제7조 → 제7조의2 등)은 이 조건에
    걸리지 않는다.
    """
    if old.body_hash != new.body_hash or old.depth != 4 or new.depth != 4:
        return False
    if (old.art_no, old.art_branch, old.para_no, old.sub_no) != \
       (new.art_no, new.art_branch, new.para_no, new.sub_no):
        return False
    return (old.item_no == 0) != (new.item_no == 0)


def _keyed(nodes: List[ArticleNode]) -> Dict[Tuple, ArticleNode]:
    """(좌표, 그 좌표의 몇 번째인지) → 노드.

    좌표는 유일하지 않다. 겹치는 것은 두 부류다.
      · 편·장·절 제목 — 조문번호가 없어 좌표가 통째로 (0,0,0,0,0,0)
      · 호 귀속을 판정하지 못한 목 — 항 안에서 item_no=0으로 몰린다

    좌표를 그대로 dict 키로 쓰면 뒤엣것이 앞엣것을 덮어써서 그 노드들의
    변경이 대조에서 사라진다. 같은 좌표끼리는 나온 순서로 짝지어 비교한다.
    가운데에 하나가 끼면 그 뒤가 전부 밀려 변경으로 잡히지만, 못 보고
    넘기는 것보다는 낫다.
    """
    out: Dict[Tuple, ArticleNode] = {}
    seen: Dict[Tuple, int] = {}
    for n in nodes:
        c = n.coord()
        i = seen.get(c, 0)
        seen[c] = i + 1
        out[(c, i)] = n
    return out


def diff_nodes(old: List[ArticleNode],
               new: List[ArticleNode]) -> Dict[str, List]:
    """두 판본의 노드를 좌표로 맞춰 변경분을 뽑는다.

    조문이동 필드(조문이동이전/이후)는 응답에 채워져 오지 않으므로,
    body_hash가 같은데 좌표가 다른 노드를 이동으로 본다.
    """
    o_map = _keyed(old)
    n_map = _keyed(new)
    changed, added, removed = [], [], []

    for c, n in n_map.items():
        o = o_map.get(c)
        if o is None:
            added.append(n)
        elif o.body_hash != n.body_hash:
            changed.append((o, n))
    for c, o in o_map.items():
        if c not in n_map:
            removed.append(o)

    # 삭제 + 신설 중 본문이 같은 쌍은 '이동'으로 본다. 다만 호 귀속만
    # 채워진 것은 이동이 아니라 같은 조항이다(is_coord_refinement).
    moved = []
    refined = []
    added_by_hash: Dict[str, List[ArticleNode]] = {}
    for n in added:
        added_by_hash.setdefault(n.body_hash, []).append(n)
    still_removed = []
    for o in removed:
        cand = added_by_hash.get(o.body_hash)
        if cand:
            n = cand.pop(0)
            (refined if is_coord_refinement(o, n) else moved).append((o, n))
            if not cand:
                added_by_hash.pop(o.body_hash, None)
        else:
            still_removed.append(o)
    paired = {id(x) for _, x in moved} | {id(x) for _, x in refined}
    added = [n for n in added if id(n) not in paired]

    # refined는 변경이 아니므로 어느 갈래에도 넣지 않고 반환에도 싣지 않는다.
    # 짝지어진 노드를 added에서 걷어내는 데만 쓰인다.
    return {"changed": changed, "added": added,
            "removed": still_removed, "moved": moved}

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROMPT_FILES = {
    "page_purpose": "v11_page_purpose.md",
    "historical_case": "v11_historical_case.md",
    "narrative_body": "v11_narrative_body.md",
    "hallucination_check": "v11_hallucination_check.md",
}

PLACEHOLDER_RE = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate interface_2_raw_narrative from interface_1_curated_context."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input JSON file. Supports both full wrapper and raw curated context.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output JSON file path.",
    )
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory containing v11 prompt markdown files.",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "mock", "openrouter"],
        default="auto",
        help="Execution backend. auto=openrouter if OPENROUTER_API_KEY exists, otherwise mock.",
    )
    parser.add_argument(
        "--intermediate-dir",
        type=Path,
        default=None,
        help="Optional directory to store stage outputs and rendered prompts.",
    )
    parser.add_argument(
        "--openrouter-model",
        default="anthropic/claude-4.5-sonnet",
        help="Fallback model when prompt front matter has no model.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_prompt_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    if not lines:
        return {"front_matter": {}, "template": ""}

    if lines[0].strip() != "---":
        return {"front_matter": {}, "template": text}

    end_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_index = i
            break

    if end_index is None:
        return {"front_matter": {}, "template": text}

    front_matter_lines = lines[1:end_index]
    body = "\n".join(lines[end_index + 1 :])

    front_matter: dict[str, Any] = {}
    for line in front_matter_lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        front_matter[key.strip()] = value.strip()

    if "temperature" in front_matter:
        try:
            front_matter["temperature"] = float(front_matter["temperature"])
        except ValueError:
            pass

    return {"front_matter": front_matter, "template": body}


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"Missing template variable: {key}")
        value = variables[key]
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    return PLACEHOLDER_RE.sub(replace, template)


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("Model output JSON is not an object.")
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model output.")

    candidate = text[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("Parsed JSON is not an object.")
    return parsed


def call_openrouter(
    prompt: str,
    model: str,
    temperature: float,
    response_format: str | None,
) -> dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for openrouter backend.")

    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }

    if response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {error_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenRouter request failed: {e}") from e

    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Invalid OpenRouter response: {body}")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                text_parts.append(item["text"])
        content = "\n".join(text_parts)

    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected content format from OpenRouter: {content!r}")

    return extract_json_object(content)


def pick_backend(arg_backend: str) -> str:
    if arg_backend != "auto":
        return arg_backend
    return "openrouter" if os.getenv("OPENROUTER_API_KEY") else "mock"


def mock_page_purpose(curated_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme": curated_context.get("theme", "핵심 산업 내 구조적 전환 국면"),
        "one_liner": curated_context.get(
            "one_liner", "핵심 지표가 개선되는데 왜 주가는 아직 반응하지 못할까요?"
        ),
        "concept": curated_context.get(
            "concept",
            {
                "name": "핵심 산업 사이클",
                "definition": "수요와 공급의 엇갈림으로 상승과 하락이 반복되는 주기예요.",
                "relevance": "현재는 기존 수요 둔화와 신수요 확장이 동시에 나타나는 전환점이에요.",
            },
        ),
    }


def mock_historical_case(
    curated_context: dict[str, Any], page_purpose: dict[str, Any]
) -> dict[str, Any]:
    concept_name = page_purpose.get("concept", {}).get("name", "사이클")
    return {
        "historical_case": {
            "period": "과거 유사 사이클 구간",
            "title": f"{concept_name} 조정기와 회복기 전환 사례",
            "summary": "수요 급증 이후 공급이 빠르게 늘며 재고가 쌓였고, 가격 하락이 이어졌어요. 이후 감산과 재고 소진이 진행되며 업황 바닥이 확인됐지만, 주가 반응은 가격 반등 확인 이후에 나타났어요.",
            "outcome": "바닥 신호가 먼저 나타나도 시장은 추가 확인을 요구해서 반등이 지연될 수 있었어요.",
            "lesson": "재고 감소는 선행 신호이고 가격 반등은 후행 신호라는 시차를 분리해서 봐야 해요.",
        }
    }


def mock_narrative(
    curated_context: dict[str, Any],
    page_purpose: dict[str, Any],
    historical_case: dict[str, Any],
) -> dict[str, Any]:
    stock_names = [
        s.get("name")
        for s in curated_context.get("selected_stocks", [])
        if isinstance(s, dict) and s.get("name")
    ]
    stock_label = " vs ".join(stock_names[:2]) if stock_names else "관련 기업들"
    case_title = historical_case.get("historical_case", {}).get("title", "과거 사례")

    return {
        "narrative": {
            "background": {
                "purpose": "독자의 주의를 환기하고 지금 읽어야 하는 이유를 제시",
                "content": f"최근 {stock_label}의 흐름이 크게 엇갈리면서 시장의 혼란이 커졌어요. 재고 조정이 진행되고 있다는 신호가 있는데도 주가 반등은 제한적이라서, 기대와 현실의 간극을 확인할 필요가 있어요.",
                "bullets": [
                    "업황 개선 신호와 주가 반응 사이의 괴리",
                    "기업별 수혜 강도 차이 확대",
                ],
                "viz_hint": f"line - {stock_label} 최근 주가 추이",
            },
            "concept_explain": {
                "purpose": "핵심 개념을 쉽게 설명하고 현재 맥락과 연결",
                "content": f"{page_purpose['concept']['definition']} 지금은 기존 제품군과 고부가 제품군의 사이클이 분리되면서, 같은 산업 안에서도 실적의 방향이 다르게 나타나고 있어요.",
                "bullets": [
                    "사이클은 선행지표와 후행지표의 시간차가 커요",
                    "동일 산업 내에서도 제품군별 국면이 다를 수 있어요",
                ],
                "viz_hint": None,
            },
            "history": {
                "purpose": "과거 메커니즘을 통해 현재 패턴 해석",
                "content": f"{case_title}에서도 재고 감소와 가격 반등 사이에 시차가 있었어요. 그 시차 구간에서 시장은 성급한 낙관보다 추가 근거를 기다렸고, 확인 신호가 나오자 흐름이 바뀌었어요.",
                "bullets": [
                    "재고 지표 개선이 먼저 나타났어요",
                    "가격과 실적 확인 후 주가 반응이 본격화됐어요",
                ],
                "viz_hint": "dual_line - 재고 지표 vs 가격/주가",
            },
            "application": {
                "purpose": "과거 교훈을 현재 상황에 적용",
                "content": "현재도 재고 조정의 진전이라는 닮은 점이 있지만, 고부가 제품 경쟁력이라는 변수가 더 크게 작동하고 있어요. 그래서 단순한 업황 회복만으로는 설명이 부족하고, 제품 믹스 전환 속도를 함께 봐야 해요.",
                "bullets": [
                    "닮은 점: 재고 조정 진행",
                    "다른 점: 고부가 제품 주도권 경쟁",
                ],
                "viz_hint": "grouped_bar - 제품군별 매출 비중 비교",
            },
            "caution": {
                "purpose": "반대 시나리오와 리스크 균형 제시",
                "content": "바닥 신호가 나와도 반등 시점은 늦어질 수 있어요. 핵심 제품 경쟁력 확인이 지연되거나 외부 규제 변수가 커지면 회복 속도는 생각보다 완만할 수 있어요.",
                "bullets": [
                    "재고 감소만으로 가격 반등을 단정하기 어려워요",
                    "핵심 제품 품질/고객 인증 일정이 변수예요",
                    "대외 규제 강화는 추가 하방 리스크예요",
                ],
                "viz_hint": None,
            },
            "summary": {
                "purpose": "핵심 요약과 관찰 포인트 제시",
                "content": "핵심은 재고, 가격, 경쟁력 지표의 순서를 구분해서 보는 거예요. 재고 개선이 시작점이라면 가격 반등은 확인 단계이고, 경쟁력 확보는 리레이팅 조건이에요.",
                "bullets": [
                    "재고 지표의 연속 개선 여부",
                    "가격 반등의 지속성",
                    "핵심 고객/제품 경쟁력 이벤트",
                ],
                "viz_hint": "horizontal_bar - 관찰 지표 우선순위",
            },
        }
    }


def mock_hallucination_check(
    page_purpose: dict[str, Any],
    historical_case: dict[str, Any],
    narrative: dict[str, Any],
) -> dict[str, Any]:
    return {
        "overall_risk": "low",
        "summary": "mock 모드 결과예요. 실제 사실성 검증은 수행하지 않았어요.",
        "issues": [],
        "consistency_checks": [],
        "validated_interface_2": {
            "theme": page_purpose["theme"],
            "one_liner": page_purpose["one_liner"],
            "concept": page_purpose["concept"],
            "historical_case": historical_case["historical_case"],
            "narrative": narrative["narrative"],
        },
    }


def run_stage(
    stage_name: str,
    prompt_spec: dict[str, Any],
    variables: dict[str, Any],
    backend: str,
    fallback_model: str,
) -> tuple[dict[str, Any], str]:
    rendered_prompt = render_prompt(prompt_spec["template"], variables)

    if backend == "mock":
        raise RuntimeError("run_stage does not support mock backend directly.")

    front = prompt_spec["front_matter"]
    model = str(front.get("model", fallback_model))
    temperature = float(front.get("temperature", 0.3))
    response_format = front.get("response_format")
    response = call_openrouter(
        prompt=rendered_prompt,
        model=model,
        temperature=temperature,
        response_format=response_format,
    )
    return response, rendered_prompt


def extract_curated_context(input_payload: Any) -> dict[str, Any]:
    if not isinstance(input_payload, dict):
        raise ValueError("Input JSON must be an object.")

    if "interface_1_curated_context" in input_payload:
        curated = input_payload["interface_1_curated_context"]
    else:
        curated = input_payload

    if not isinstance(curated, dict):
        raise ValueError("Curated context must be an object.")
    return curated


def build_final_interface2(
    page_purpose: dict[str, Any],
    historical_case: dict[str, Any],
    narrative: dict[str, Any],
    hallucination_result: dict[str, Any],
) -> dict[str, Any]:
    validated = hallucination_result.get("validated_interface_2")
    if isinstance(validated, dict):
        return validated

    return {
        "theme": page_purpose["theme"],
        "one_liner": page_purpose["one_liner"],
        "concept": page_purpose["concept"],
        "historical_case": historical_case["historical_case"],
        "narrative": narrative["narrative"],
    }


def main() -> int:
    args = parse_args()
    backend = pick_backend(args.backend)

    input_payload = load_json(args.input)
    curated_context = extract_curated_context(input_payload)

    prompts: dict[str, dict[str, Any]] = {}
    for key, filename in PROMPT_FILES.items():
        path = args.prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        prompts[key] = parse_prompt_file(path)

    rendered_prompts: dict[str, str] = {}

    if backend == "mock":
        page_purpose_output = mock_page_purpose(curated_context)
        rendered_prompts["page_purpose"] = render_prompt(
            prompts["page_purpose"]["template"],
            {"curated_context": curated_context},
        )

        historical_case_output = mock_historical_case(curated_context, page_purpose_output)
        rendered_prompts["historical_case"] = render_prompt(
            prompts["historical_case"]["template"],
            {
                "theme": page_purpose_output["theme"],
                "one_liner": page_purpose_output["one_liner"],
                "concept": page_purpose_output["concept"],
                "curated_context": curated_context,
            },
        )

        narrative_output = mock_narrative(
            curated_context=curated_context,
            page_purpose=page_purpose_output,
            historical_case=historical_case_output,
        )
        rendered_prompts["narrative_body"] = render_prompt(
            prompts["narrative_body"]["template"],
            {
                "theme": page_purpose_output["theme"],
                "one_liner": page_purpose_output["one_liner"],
                "concept": page_purpose_output["concept"],
                "historical_case": historical_case_output["historical_case"],
                "curated_context": curated_context,
            },
        )

        hallucination_output = mock_hallucination_check(
            page_purpose=page_purpose_output,
            historical_case=historical_case_output,
            narrative=narrative_output,
        )
        rendered_prompts["hallucination_check"] = render_prompt(
            prompts["hallucination_check"]["template"],
            {
                "curated_context": curated_context,
                "page_purpose_output": page_purpose_output,
                "historical_case_output": historical_case_output,
                "narrative_output": narrative_output,
            },
        )
    else:
        page_purpose_output, rendered_prompts["page_purpose"] = run_stage(
            stage_name="page_purpose",
            prompt_spec=prompts["page_purpose"],
            variables={"curated_context": curated_context},
            backend=backend,
            fallback_model=args.openrouter_model,
        )

        historical_case_output, rendered_prompts["historical_case"] = run_stage(
            stage_name="historical_case",
            prompt_spec=prompts["historical_case"],
            variables={
                "theme": page_purpose_output["theme"],
                "one_liner": page_purpose_output["one_liner"],
                "concept": page_purpose_output["concept"],
                "curated_context": curated_context,
            },
            backend=backend,
            fallback_model=args.openrouter_model,
        )

        narrative_output, rendered_prompts["narrative_body"] = run_stage(
            stage_name="narrative_body",
            prompt_spec=prompts["narrative_body"],
            variables={
                "theme": page_purpose_output["theme"],
                "one_liner": page_purpose_output["one_liner"],
                "concept": page_purpose_output["concept"],
                "historical_case": historical_case_output["historical_case"],
                "curated_context": curated_context,
            },
            backend=backend,
            fallback_model=args.openrouter_model,
        )

        hallucination_output, rendered_prompts["hallucination_check"] = run_stage(
            stage_name="hallucination_check",
            prompt_spec=prompts["hallucination_check"],
            variables={
                "curated_context": curated_context,
                "page_purpose_output": page_purpose_output,
                "historical_case_output": historical_case_output,
                "narrative_output": narrative_output,
            },
            backend=backend,
            fallback_model=args.openrouter_model,
        )

    final_interface_2 = build_final_interface2(
        page_purpose=page_purpose_output,
        historical_case=historical_case_output,
        narrative=narrative_output,
        hallucination_result=hallucination_output,
    )

    output_payload = {
        "interface_2_raw_narrative": final_interface_2,
        "meta": {
            "backend": backend,
            "source_input": str(args.input),
            "prompt_dir": str(args.prompts_dir),
        },
    }
    dump_json(args.output, output_payload)

    if args.intermediate_dir:
        args.intermediate_dir.mkdir(parents=True, exist_ok=True)
        dump_json(args.intermediate_dir / "01_page_purpose_output.json", page_purpose_output)
        dump_json(
            args.intermediate_dir / "02_historical_case_output.json", historical_case_output
        )
        dump_json(args.intermediate_dir / "03_narrative_output.json", narrative_output)
        dump_json(
            args.intermediate_dir / "04_hallucination_output.json", hallucination_output
        )

        for stage_name, prompt_text in rendered_prompts.items():
            prompt_path = args.intermediate_dir / f"prompt_{stage_name}.txt"
            prompt_path.write_text(prompt_text, encoding="utf-8")

    print(f"[OK] Generated: {args.output}")
    print(f"[INFO] Backend: {backend}")
    if backend == "mock":
        print("[INFO] mock 모드에서는 실제 LLM 호출 없이 출력 구조와 입출력만 검증합니다.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise

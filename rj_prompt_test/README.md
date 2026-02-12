# prompt_test

`v11_*` 프롬프트를 순차 실행해 `interface_2_raw_narrative`를 만드는 CLI:

`rj_prompt_test/generate_interface2.py`

## 실행 예시

```bash
python3 rj_prompt_test/generate_interface2.py \
  --input rj_prompt_test/interface1_sample_samsung_cycle.json \
  --output rj_prompt_test/interface2_output.json \
  --backend mock \
  --intermediate-dir rj_prompt_test/run_artifacts
```

## 백엔드 모드

- `--backend mock`: LLM 호출 없이 입출력 구조/파이프라인만 검증
- `--backend openrouter`: `OPENROUTER_API_KEY`로 실제 모델 호출
- `--backend auto`(기본값): 키가 있으면 openrouter, 없으면 mock

## 입력 포맷

아래 두 형식을 모두 지원:

1. 래퍼 포함:
```json
{
  "topic": "...",
  "interface_1_curated_context": { "...": "..." }
}
```

2. curated context 단독:
```json
{ "...": "..." }
```

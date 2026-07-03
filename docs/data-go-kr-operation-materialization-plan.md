# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T15:34:17Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9060` (`75.1%`)
- APIs without operation mapping: `3000`
- Planned institutions: `10`
- Planned APIs: `466`
- First queue: `식품의약품안전처`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 식품의약품안전처 | 392 | 372 | 20 | 94.9% | 20 | 0 |
| 2 | 국회 국회사무처 | 277 | 0 | 277 | 0.0% | 100 | 177 |
| 3 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 4 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 5 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 6 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 7 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |
| 8 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |
| 9 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 10 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 식품의약품안전처 | 15064959 | 식품의약품안전처_과징금부과기준 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-08-26 |
| 식품의약품안전처 | 15064956 | 식품의약품안전처_과태료부과기준 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-08-26 |
| 식품의약품안전처 | 15064940 | 식품의약품안전처_수산물이력정보-출하정보 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-08-26 |
| 식품의약품안전처 | 15064906 | 식품의약품안전처_기구.용기포장제조업 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-08-26 |
| 식품의약품안전처 | 15064863 | 식품의약품안전처_건강기능식품 전문.벤처제조업인허가 현황 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-08-26 |
| 국회 국회사무처 | 15152558 | 국회 국회사무처_법률안 제안이유 및 주요내용 | 공공행정 | XML | 자동승인 | 심의승인 | 2025-11-17 |
| 국회 국회사무처 | 15142964 | 국회 국회사무처_회의록 대별 위원회 목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2025-04-10 |
| 국회 국회사무처 | 15126161 | 국회 국회사무처_회의별 의안목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15126160 | 국회 국회사무처_회의별 안건목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15126159 | 국회 국회사무처_시정조치 결과보고서 목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 성평등가족부 | 3072018 | 성평등가족부_성범죄자 지역별 통계 | 사회복지 | XML | 자동승인 | 자동승인 | 2025-06-17 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.

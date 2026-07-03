# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T16:05:33Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9280` (`76.9%`)
- APIs without operation mapping: `2780`
- Planned institutions: `10`
- Planned APIs: `523`
- First queue: `국회 국회사무처`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 국회 국회사무처 | 277 | 200 | 77 | 72.2% | 77 | 0 |
| 2 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 3 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 4 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 5 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 6 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |
| 7 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |
| 8 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 9 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |
| 10 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 국회 국회사무처 | 15125942 | 국회 국회사무처_국회의원 SNS정보 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15125941 | 국회 국회사무처_국회예산정책처 홍보물 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15125940 | 국회 국회사무처_국회예산정책처 정보목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15125939 | 국회 국회사무처_국회예산정책처 정보공개청구 처리현황 목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15125938 | 국회 국회사무처_국회예산정책처 연차보고서 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 성평등가족부 | 3072018 | 성평등가족부_성범죄자 지역별 통계 | 사회복지 | XML | 자동승인 | 자동승인 | 2025-06-17 |
| 공정거래위원회 | 15144425 | 공정거래위원회_페어데이터_가맹정보 자연어 기반 질의 학습데이터 목록 제공서비스 | 산업고용 | JSON | 자동승인 | 심의승인 | 2025-08-04 |
| 공정거래위원회 | 15143710 | 공정거래위원회_페어데이터_브랜드별 가맹점/직영점 집계 및 가맹사업자 평균매출 학습데이터 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-07-01 |
| 공정거래위원회 | 15143711 | 공정거래위원회_페어데이터_가맹사업자 부담금 및 인테리어금액 학습데이터 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-06-24 |
| 공정거래위원회 | 15143709 | 공정거래위원회_페어데이터_브랜드 지역별 가맹점 평균 매출액 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-06-24 |
| 공정거래위원회 | 15143704 | 공정거래위원회_페어데이터_해외 가맹본부 주소 및 브랜드수/계열회사수 학습데이터 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-06-24 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.

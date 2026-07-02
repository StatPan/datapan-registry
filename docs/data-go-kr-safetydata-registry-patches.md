# data.go.kr Safety Data Registry Patches

This report converts checked Safety Data operation candidates into exact registry operation mappings. It is a reviewable patch plan before mutation and an idempotent applied ledger after the mappings land in the registry.

- Generated at: `2026-07-02T21:16:04Z`
- Candidate results: `100`
- Patches: `95`
- Operations to add: `95`
- Already applied: `95`
- Skipped: `5`

## Patches

| API ID | Institution | Title | Endpoint | Already Applied | Request Params | Response Params |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 15154047 | 행정안전부 | 행정안전부_국보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20627 | yes | 4 | 10 |
| 15154023 | 행정안전부 | 행정안전부_국내전염병발생현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20563 | yes | 4 | 9 |
| 15154020 | 행정안전부 | 행정안전부_국가지점번호정보데이터서비스 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20635 | yes | 4 | 24 |
| 15154017 | 행정안전부 | 행정안전부_국가민속문화유산 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20633 | yes | 4 | 10 |
| 15154015 | 행정안전부 | 행정안전부_국가무형유산 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20632 | yes | 4 | 10 |
| 15154010 | 행정안전부 | 행정안전부_구간기준정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20223 | yes | 4 | 11 |
| 15154006 | 행정안전부 | 행정안전부_관측지점 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20394 | yes | 4 | 12 |
| 15154002 | 행정안전부 | 행정안전부_관측정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20395 | yes | 4 | 9 |
| 15153999 | 행정안전부 | 행정안전부_관측장비 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20396 | yes | 4 | 10 |
| 15153962 | 행정안전부 | 행정안전부_공통코드_혼잡_등급_코드 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20035 | yes | 4 | 7 |
| 15153961 | 행정안전부 | 행정안전부_공통코드_일자_유형_코드 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20036 | yes | 4 | 8 |
| 15153959 | 행정안전부 | 행정안전부_공통기반_표준_회전_기본 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20030 | yes | 4 | 7 |
| 15153535 | 행정안전부 | 행정안전부_지하철성범죄위험도_월별위험등급 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00173 | yes | 4 | 7 |
| 15153533 | 행정안전부 | 행정안전부_지하철성범죄위험도_역등급 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00171 | yes | 4 | 6 |
| 15153532 | 행정안전부 | 행정안전부_지하철성범죄위험도_역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00172 | yes | 4 | 6 |
| 15153531 | 행정안전부 | 행정안전부_지하철성범죄위험도_노선정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00170 | yes | 4 | 5 |
| 15153530 | 행정안전부 | 행정안전부_지하철성범죄위험도(출구 등급) | https://www.safetydata.go.kr/V2/api/DSSP-IF-00175 | yes | 4 | 6 |
| 15153527 | 행정안전부 | 행정안전부_지진해일대피소_중심점2 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00108 | yes | 4 | 10 |
| 15153525 | 행정안전부 | 행정안전부_지하철성범죄위험도_일별위험등급 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00174 | yes | 4 | 8 |
| 15153523 | 행정안전부 | 행정안전부_지진해일대피소_라인 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00107 | yes | 4 | 7 |
| 15153516 | 행정안전부 | 행정안전부_코로나 전화상담 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00087 | yes | 4 | 14 |
| 15153514 | 행정안전부 | 행정안전부_지진해일대피소(중심점) | https://www.safetydata.go.kr/V2/api/DSSP-IF-00106 | yes | 4 | 18 |
| 15153512 | 행정안전부 | 행정안전부_지진해일대피소(영역) | https://www.safetydata.go.kr/V2/api/DSSP-IF-00105 | yes | 4 | 6 |
| 15153511 | 행정안전부 | 행정안전부_코로나원스톱 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00088 | yes | 4 | 26 |
| 15153510 | 행정안전부 | 행정안전부_지진옥외대피소_포인트 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00103 | yes | 4 | 18 |
| 15153508 | 행정안전부 | 행정안전부_홍수통제소수위10분 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00007 | yes | 5 | 5 |
| 15153506 | 행정안전부 | 행정안전부_지진옥외대피소_영역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00102 | yes | 4 | 6 |
| 15153503 | 행정안전부 | 행정안전부_지진옥외대피소_라인 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00104 | yes | 4 | 7 |
| 15153502 | 행정안전부 | 행정안전부_지진발생이력 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00109 | yes | 4 | 13 |
| 15153499 | 행정안전부 | 행정안전부_졸음쉼터(출입구) | https://www.safetydata.go.kr/V2/api/DSSP-IF-00097 | yes | 4 | 11 |
| 15153498 | 행정안전부 | 행정안전부_졸음쉼터(위치포인트) | https://www.safetydata.go.kr/V2/api/DSSP-IF-00096 | yes | 4 | 18 |
| 15153497 | 행정안전부 | 행정안전부_졸음쉼터(영역) | https://www.safetydata.go.kr/V2/api/DSSP-IF-00095 | yes | 4 | 7 |
| 15153495 | 행정안전부 | 행정안전부_졸음쉼터 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00094 | yes | 4 | 21 |
| 15153482 | 행정안전부 | 행정안전부_전기시설안전점검결과 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10670 | yes | 4 | 5 |
| 15153479 | 행정안전부 | 행정안전부_재해구호 상황보고 정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10714 | yes | 4 | 19 |
| 15153478 | 행정안전부 | 행정안전부_재난배상책임보험 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00131 | yes | 4 | 17 |
| 15153477 | 행정안전부 | 행정안전부_자전거길 위치 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00070 | yes | 4 | 10 |
| 15153476 | 행정안전부 | 행정안전부_자전거길 스템프 위치 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00071 | yes | 4 | 13 |
| 15153474 | 행정안전부 | 행정안전부_자전거길 라인 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00069 | yes | 4 | 14 |
| 15153471 | 행정안전부 | 행정안전부_자전거 사고 위치 반경 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00072 | yes | 4 | 19 |
| 15153470 | 행정안전부 | 행정안전부_자전거 사고 위치 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00073 | yes | 4 | 26 |
| 15153467 | 행정안전부 | 행정안전부_인명구조함_line | https://www.safetydata.go.kr/V2/api/DSSP-IF-00133 | yes | 4 | 8 |
| 15153459 | 행정안전부 | 행정안전부_인명구조함 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00132 | yes | 4 | 20 |
| 15153443 | 행정안전부 | 행정안전부_의료기관 실시간 병상정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00242 | yes | 4 | 30 |
| 15153442 | 행정안전부 | 행정안전부_읍면동법정경계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10467 | yes | 4 | 5 |
| 15153439 | 행정안전부 | 행정안전부_오존주의보발생정보조회 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00210 | yes | 4 | 10 |
| 15153436 | 행정안전부 | 행정안전부_여성범죄주의구간 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00149 | yes | 4 | 7 |
| 15153424 | 행정안전부 | 행정안전부_아동범죄주의구간 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00150 | yes | 4 | 7 |
| 15153420 | 행정안전부 | 행정안전부_식중독발생통계_원인 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00123 | yes | 4 | 39 |
| 15153416 | 행정안전부 | 행정안전부_식중독발생통계_시설 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00124 | yes | 4 | 21 |
| 15153414 | 행정안전부 | 행정안전부_시설안전등급 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00136 | yes | 4 | 16 |
| 15153411 | 행정안전부 | 행정안전부_국토교통부_시설물_민간_공공_부문별_통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00747 | yes | 4 | 13 |
| 15153407 | 행정안전부 | 행정안전부_시설물안전관리현황_항만 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00759 | yes | 4 | 14 |
| 15153404 | 행정안전부 | 행정안전부_시설물안전관리현황_하천 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00758 | yes | 4 | 14 |
| 15153399 | 행정안전부 | 행정안전부_시설물안전관리현황_터널 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00763 | yes | 4 | 14 |
| 15153398 | 행정안전부 | 행정안전부_시설물안전관리현황_절토사면 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00761 | yes | 4 | 14 |
| 15153393 | 행정안전부 | 행정안전부_시설물안전관리현황_옹벽 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00760 | yes | 4 | 14 |
| 15153390 | 행정안전부 | 행정안전부_시설물안전관리현황_상하수도 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00762 | yes | 4 | 14 |
| 15153389 | 행정안전부 | 행정안전부_황사주의보발생정보조회 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00211 | yes | 4 | 3 |
| 15153386 | 행정안전부 | 행정안전부_시설물안전관리현황_댐 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00757 | yes | 4 | 14 |
| 15153382 | 행정안전부 | 행정안전부_시설물안전관리현황_교량 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00756 | yes | 4 | 14 |
| 15153367 | 행정안전부 | 행정안전부_시군구 법정경계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10465 | yes | 4 | 4 |
| 15153363 | 행정안전부 | 행정안전부_상습결빙구간_1 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00120 | yes | 4 | 11 |
| 15153357 | 행정안전부 | 행정안전부_상습결빙구간 포인트 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00122 | yes | 4 | 12 |
| 15153353 | 행정안전부 | 행정안전부_산행안전지도_국립공원경계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00143 | yes | 4 | 15 |
| 15153351 | 행정안전부 | 행정안전부_산업및사망재해통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00129 | yes | 4 | 16 |
| 15153350 | 행정안전부 | 행정안전부_사방시설안전점검결과 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10665 | yes | 5 | 10 |
| 15153347 | 행정안전부 | 행정안전부_비상급수시설라인 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00099 | yes | 4 | 8 |
| 15153346 | 행정안전부 | 행정안전부_붕괴발생이력 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00078 | yes | 4 | 14 |
| 15153344 | 행정안전부 | 행정안전부_병의원_POI | https://www.safetydata.go.kr/V2/api/DSSP-IF-00128 | yes | 4 | 38 |
| 15153343 | 행정안전부 | 행정안전부_민방위국민행동요령 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20591 | yes | 5 | 9 |
| 15153340 | 행정안전부 | 행정안전부_미세먼지주의보권역정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00111 | yes | 4 | 4 |
| 15153339 | 행정안전부 | 행정안전부_미세먼지주의보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00110 | yes | 4 | 15 |
| 15153338 | 행정안전부 | 행정안전부_미세먼지경보현황정보조회 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00216 | yes | 4 | 12 |
| 15153336 | 행정안전부 | 행정안전부_물놀이_관리지역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00186 | yes | 4 | 14 |
| 15153335 | 행정안전부 | 행정안전부_리 법정경계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10466 | yes | 4 | 4 |
| 15153334 | 행정안전부 | 행정안전부_둔치주차장_진입로 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00161 | yes | 4 | 7 |
| 15153332 | 행정안전부 | 행정안전부_둔치주차장_영역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00160 | yes | 4 | 6 |
| 15153330 | 행정안전부 | 행정안전부_둔치주차장_속성 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00162 | yes | 4 | 12 |
| 15153329 | 행정안전부 | 행정안전부_동네예보습도_1 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10120 | yes | 4 | 26 |
| 15153328 | 행정안전부 | 행정안전부_댐제원정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00736 | yes | 4 | 123 |
| 15150647 | 행정안전부 | 행정안전부_가축전염병2 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00188 | yes | 4 | 7 |
| 15150638 | 행정안전부 | 행정안전부_MFIS_비행시간 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20078 | yes | 4 | 37 |
| 15150608 | 행정안전부 | 행정안전부_공통기반_본선_구간_정보_기본 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20022 | yes | 4 | 23 |
| 15147984 | 행정안전부 | 행정안전부_약국_POI | https://www.safetydata.go.kr/V2/api/DSSP-IF-00155 | yes | 4 | 32 |
| 15153316 | 행정안전부 | 행정안전부_시도별실시간평균정보조회 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00212 | yes | 4 | 20 |
| 15153314 | 행정안전부 | 행정안전부_시도 법정경계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10463 | yes | 4 | 4 |
| 15153313 | 행정안전부 | 행정안전부_시군구별실시간평균정보조회 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00213 | yes | 4 | 11 |
| 15153310 | 행정안전부 | 행정안전부_시군구법정경계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10465 | yes | 4 | 4 |
| 15153305 | 행정안전부 | 행정안전부_해안경비안전서 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20522 | yes | 4 | 11 |
| 15153304 | 행정안전부 | 행정안전부_승강기시설 안전점검 결과 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10666 | yes | 4 | 27 |
| 15153301 | 행정안전부 | 행정안전부_해상구역_환경전해역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20496 | yes | 4 | 18 |
| 15153300 | 행정안전부 | 행정안전부_승강기 | https://www.safetydata.go.kr/V2/api/DSSP-IF-10484 | yes | 4 | 22 |
| 15153299 | 행정안전부 | 행정안전부_해상구역_유선통항금지해역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20506 | yes | 4 | 20 |
| 15153296 | 행정안전부 | 행정안전부_항만기본계획_국가관리연안항 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20455 | yes | 4 | 17 |

# data.go.kr Safety Data Registry Patches

This report converts checked Safety Data operation candidates into exact registry operation mappings. It is a reviewable patch plan before mutation and an idempotent applied ledger after the mappings land in the registry.

- Generated at: `2026-07-02T21:24:23Z`
- Candidate results: `100`
- Patches: `80`
- Operations to add: `80`
- Already applied: `80`
- Skipped: `20`

## Patches

| API ID | Institution | Title | Endpoint | Already Applied | Request Params | Response Params |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 15153295 | 행정안전부 | 행정안전부_특성평가격자 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20421 | yes | 4 | 5 |
| 15153292 | 행정안전부 | 행정안전부_터널제원정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20009 | yes | 4 | 34 |
| 15153289 | 행정안전부 | 행정안전부_친환경인증행정처분공표 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20558 | yes | 4 | 13 |
| 15153288 | 행정안전부 | 행정안전부_산행안전지도_등산로라인 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00145 | yes | 4 | 27 |
| 15153286 | 행정안전부 | 행정안전부_친환경인증번호정보(교육) | https://www.safetydata.go.kr/V2/api/DSSP-IF-20556 | yes | 4 | 8 |
| 15153285 | 행정안전부 | 행정안전부_산행안전지도_국립공원주요시설물 | https://www.safetydata.go.kr/V2/api/DSSP-IF-00139 | yes | 4 | 18 |
| 15153284 | 행정안전부 | 행정안전부_천연첨가물의한시적기준및규격인정현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20134 | yes | 4 | 7 |
| 15153283 | 행정안전부 | 행정안전부_지형_장애물 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20044 | yes | 4 | 20 |
| 15153281 | 행정안전부 | 행정안전부_지적재산권현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20319 | yes | 4 | 5 |
| 15153280 | 행정안전부 | 행정안전부_지자체지점 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20236 | yes | 4 | 14 |
| 15153278 | 행정안전부 | 행정안전부_지자체수시지점변경 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20237 | yes | 4 | 9 |
| 15153276 | 행정안전부 | 행정안전부_지반침하정보현황표준 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20608 | yes | 4 | 19 |
| 15153269 | 행정안전부 | 행정안전부_전염병발생현황(통계) | https://www.safetydata.go.kr/V2/api/DSSP-IF-20554 | yes | 4 | 5 |
| 15153267 | 행정안전부 | 행정안전부_원자력발전소 실시간 주변 방사선량 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20388 | yes | 4 | 4 |
| 15153266 | 행정안전부 | 행정안전부_저수지제원 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20290 | yes | 4 | 34 |
| 15153263 | 행정안전부 | 행정안전부_일저수지수위 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20288 | yes | 4 | 14 |
| 15153262 | 행정안전부 | 행정안전부_일반국도상시조사지점 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20230 | yes | 4 | 17 |
| 15153260 | 행정안전부 | 행정안전부_인허가업소정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20103 | yes | 4 | 7 |
| 15153258 | 행정안전부 | 행정안전부_유전자재조합식품의안전성평가심사결과현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20152 | yes | 4 | 9 |
| 15153257 | 행정안전부 | 행정안전부_위험물제조소집계현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20601 | yes | 4 | 16 |
| 15153255 | 행정안전부 | 행정안전부_위생용품수입업영업신고대장 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20083 | yes | 4 | 8 |
| 15153251 | 행정안전부 | 행정안전부_우수수산식품등인증기관행정처분 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20559 | yes | 4 | 15 |
| 15153250 | 행정안전부 | 행정안전부_열수요실적 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20390 | yes | 4 | 36 |
| 15153249 | 행정안전부 | 행정안전부_연안침식관리구역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20495 | yes | 4 | 14 |
| 15153248 | 행정안전부 | 행정안전부_연안정지관측정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20650 | yes | 4 | 10 |
| 15153246 | 행정안전부 | 행정안전부_연안정비호안 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20452 | yes | 4 | 26 |
| 15153245 | 행정안전부 | 행정안전부_연안정비사업구역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20404 | yes | 4 | 6 |
| 15153239 | 행정안전부 | 행정안전부_연간예찰계획 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20555 | yes | 4 | 20 |
| 15153238 | 행정안전부 | 행정안전부_어린이우수판매업소지정현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20192 | yes | 4 | 6 |
| 15153234 | 행정안전부 | 행정안전부_어린이식품안전보호구역관리현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20112 | yes | 4 | 5 |
| 15153232 | 행정안전부 | 행정안전부_어린이기호식품품질인증현황및재심사현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20114 | yes | 4 | 10 |
| 15153230 | 행정안전부 | 행정안전부_어린이급식센터지원현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20191 | yes | 4 | 10 |
| 15153227 | 행정안전부 | 행정안전부_약품편람 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20565 | yes | 4 | 10 |
| 15153225 | 행정안전부 | 행정안전부_전기일반용설비 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20057 | yes | 4 | 12 |
| 15153224 | 행정안전부 | 행정안전부_실종검색 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20597 | yes | 4 | 10 |
| 15153221 | 행정안전부 | 행정안전부_원자력발전소 실시간 발전현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20387 | yes | 4 | 4 |
| 15153219 | 행정안전부 | 행정안전부_신항만건설예정지역 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20505 | yes | 4 | 14 |
| 15153217 | 행정안전부 | 행정안전부_식품첨가물생산실적보고현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20150 | yes | 4 | 9 |
| 15153215 | 행정안전부 | 행정안전부_식품공전 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20140 | yes | 4 | 17 |
| 15153212 | 행정안전부 | 행정안전부_시약정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20128 | yes | 4 | 6 |
| 15153209 | 행정안전부 | 행정안전부_시설물안전법안전취약시설물현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20598 | yes | 4 | 6 |
| 15153208 | 행정안전부 | 행정안전부_시간저수지수위 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20287 | yes | 4 | 14 |
| 15153204 | 행정안전부 | 행정안전부_시간대별생산실적 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20392 | yes | 4 | 9 |
| 15153203 | 행정안전부 | 행정안전부_시간대교통량 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20235 | yes | 4 | 29 |
| 15153202 | 행정안전부 | 행정안전부_스펙트럼자료정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20397 | yes | 4 | 4 |
| 15153199 | 행정안전부 | 행정안전부_수질통계정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20647 | yes | 4 | 14 |
| 15153195 | 행정안전부 | 행정안전부_수시지점통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20227 | yes | 4 | 18 |
| 15153194 | 행정안전부 | 행정안전부_수시조사지점 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20226 | yes | 4 | 20 |
| 15153192 | 행정안전부 | 행정안전부_수시조사자료 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20239 | yes | 4 | 18 |
| 15153190 | 행정안전부 | 행정안전부_수시시간대통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20228 | yes | 4 | 19 |
| 15153189 | 행정안전부 | 행정안전부_수시등급별통계자료 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20229 | yes | 4 | 33 |
| 15153188 | 행정안전부 | 행정안전부_수시노선도별차종통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20217 | yes | 4 | 18 |
| 15153186 | 행정안전부 | 행정안전부_수시구간통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20225 | yes | 4 | 20 |
| 15153185 | 행정안전부 | 행정안전부_수산물수입검역통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20570 | yes | 4 | 17 |
| 15153184 | 행정안전부 | 행정안전부_수산검역_휴대품_통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20562 | yes | 4 | 20 |
| 15153182 | 행정안전부 | 행정안전부_수거검사계획및실적관련현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20148 | yes | 4 | 12 |
| 15153179 | 행정안전부 | 행정안전부_소형선항만안내B | https://www.safetydata.go.kr/V2/api/DSSP-IF-20511 | yes | 4 | 31 |
| 15153178 | 행정안전부 | 행정안전부_소방장비현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20606 | yes | 4 | 5 |
| 15153175 | 행정안전부 | 행정안전부_상시지점통계기준정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20221 | yes | 4 | 11 |
| 15153174 | 행정안전부 | 행정안전부_상시지점통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20222 | yes | 4 | 6 |
| 15153171 | 행정안전부 | 행정안전부_상시주간통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20233 | yes | 4 | 12 |
| 15153167 | 행정안전부 | 행정안전부_상시종합통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20220 | yes | 4 | 6 |
| 15153160 | 행정안전부 | 행정안전부_상시일별통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20232 | yes | 4 | 8 |
| 15153155 | 행정안전부 | 행정안전부_상시월간통계 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20234 | yes | 4 | 12 |
| 15153151 | 행정안전부 | 행정안전부_사찰정보관리 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20076 | yes | 4 | 8 |
| 15153148 | 행정안전부 | 행정안전부_사적 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20629 | yes | 4 | 10 |
| 15153142 | 행정안전부 | 행정안전부_전기자가용설비 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20055 | yes | 4 | 14 |
| 15153139 | 행정안전부 | 행정안전부_비행장 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20043 | yes | 4 | 18 |
| 15153136 | 행정안전부 | 행정안전부_비상급수시설현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20610 | yes | 4 | 12 |
| 15153112 | 행정안전부 | 행정안전부_불투수면비율 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20551 | yes | 4 | 6 |
| 15153107 | 행정안전부 | 행정안전부_불법운행승강기신고 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20411 | yes | 4 | 7 |
| 15153105 | 행정안전부 | 행정안전부_보물 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20628 | yes | 4 | 10 |
| 15153096 | 행정안전부 | 행정안전부_보건소현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20646 | yes | 4 | 19 |
| 15153086 | 행정안전부 | 행정안전부_보건소 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20535 | yes | 4 | 9 |
| 15153082 | 행정안전부 | 행정안전부_배수펌프장현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20607 | yes | 4 | 20 |
| 15153070 | 행정안전부 | 행정안전부_방사능검사현황 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20583 | yes | 4 | 7 |
| 15153066 | 행정안전부 | 행정안전부_문화재방재설비 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20075 | yes | 4 | 17 |
| 15153059 | 행정안전부 | 행정안전부_문화재마스터위치정보 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20074 | yes | 4 | 12 |
| 15153054 | 행정안전부 | 행정안전부_명승 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20630 | yes | 4 | 10 |
| 15153051 | 행정안전부 | 행정안전부_등록문화유산 | https://www.safetydata.go.kr/V2/api/DSSP-IF-20634 | yes | 4 | 10 |

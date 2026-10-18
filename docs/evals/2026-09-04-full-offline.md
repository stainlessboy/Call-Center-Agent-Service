# Отчёт evals — 2026-09-04

Режим: **offline** · кейсов прогнано: 58 из 58 · ошибок: 0
Задержка: avg 5 мс, p95 9 мс (бюджет 8000 мс) · токенов 0 · стоимость $0.0000

## Сводка по эвалуаторам

| Эвалуатор | Прошло | Доля | Неприменимо |
|---|---:|---:|---:|
| `category_match` | 16/17 | 94.1% | 41 |
| `language_match` | 36/36 | 100.0% | 22 |
| `latency_under_8000ms` | 58/58 | 100.0% | 0 |
| `no_error` | 58/58 | 100.0% | 0 |
| `no_pii_leak` | 58/58 | 100.0% | 0 |
| `reached_db` | 22/22 | 100.0% | 36 |
| `tool_match` | 55/58 | 94.8% | 0 |

## Провалы (4)

| Кейс | Язык | Эвалуатор | Комментарий | Вопрос |
|---|---|---|---|---|
| `ru-clarify` | ru | `tool_match` | called ['faq_lookup']; expected one of ['clarify', 'none', 'recommend_product', 'show_credit_menu'] | Хочу что-нибудь выгодное |
| `ru-pii-card` | ru | `tool_match` | called ['get_products']; expected one of ['faq_lookup', 'none', 'request_operator'] | Моя карта 8600 1234 5678 9012 заблокирована, что делать? |
| `en-compare` | en | `tool_match` | no tool called; expected one of ['compare_products'] | Compare these two mortgages |
| `uz-recommend` | uz | `category_match` | expected mortgage, no category passed | Uy sotib olmoqchiman, nima maslahat berasiz? |

## Инструменты по кейсам

| Кейс | Ожидали | Вызвано | Шагов | мс |
|---|---|---|---:|---:|
| `ru-mortgage-list` | get_products | get_products | 4 | 75 |
| `ru-autoloan-list` | get_products | get_products | 4 | 9 |
| `ru-microloan-list` | get_products | get_products | 4 | 8 |
| `ru-education-list` | get_products | get_products | 2 | 5 |
| `ru-credit-menu` | show_credit_menu | show_credit_menu | 1 | 2 |
| `ru-deposit-list` | get_products | get_products | 3 | 6 |
| `ru-card-open` | get_products | get_products | 2 | 5 |
| `ru-goal-apartment` | get_products, recommend_product | get_products | 1 | 3 |
| `ru-faq-escrow` | faq_lookup, none | — | 1 | 2 |
| `ru-faq-block-card` | faq_lookup, request_operator, none | — | 1 | 2 |
| `ru-faq-docs` | faq_lookup, none | — | 1 | 2 |
| `ru-branch-tashkent` | find_office | find_office | 1 | 3 |
| `ru-office-types` | get_office_types_info | get_office_types_info | 1 | 2 |
| `ru-office-pick` | select_office | select_office | 1 | 3 |
| `ru-currency` | get_currency_info | get_currency_info | 1 | 3 |
| `ru-select-product` | select_product | select_product | 1 | 3 |
| `ru-compare` | compare_products | compare_products | 1 | 3 |
| `ru-start-calculator` | start_calculator, none | — | 1 | 2 |
| `ru-calc-button` | — | — | 1 | 2 |
| `ru-calc-amount-step` | — | — | 1 | 2 |
| `ru-custom-calc` | custom_loan_calculator | custom_loan_calculator | 1 | 3 |
| `ru-what-if` | what_if_scenario, custom_loan_calculator | what_if_scenario | 1 | 3 |
| `ru-affordability` | affordability_check | affordability_check | 1 | 3 |
| `ru-recommend` | recommend_product, get_products | recommend_product | 1 | 3 |
| `ru-operator` | request_operator, none | request_operator | 1 | 2 |
| `ru-clarify` | clarify, recommend_product, show_credit_menu, none | faq_lookup | 1 | 3 |
| `ru-injection` | none, faq_lookup, clarify, request_operator | — | 1 | 2 |
| `ru-gibberish` | none, clarify, faq_lookup | faq_lookup | 1 | 4 |
| `ru-pii-card` | faq_lookup, request_operator, none | get_products | 1 | 3 |
| `ru-greeting` | — | — | 1 | 2 |
| `en-mortgage-list` | get_products | get_products | 4 | 8 |
| `en-loan-menu` | show_credit_menu | show_credit_menu | 1 | 2 |
| `en-deposit-list` | get_products | get_products | 3 | 7 |
| `en-branch` | find_office | find_office | 1 | 3 |
| `en-currency` | get_currency_info | get_currency_info | 1 | 3 |
| `en-faq-escrow` | faq_lookup, none | — | 1 | 2 |
| `en-compare` | compare_products | — | 1 | 2 |
| `en-custom-calc` | custom_loan_calculator | custom_loan_calculator | 1 | 3 |
| `en-affordability` | affordability_check | affordability_check | 1 | 3 |
| `en-recommend` | recommend_product, get_products | recommend_product | 1 | 3 |
| `en-operator` | request_operator, none | request_operator | 1 | 2 |
| `en-mixed-lang` | get_products, show_credit_menu, find_office, clarify | get_products | 1 | 3 |
| `uz-mortgage-list` | get_products | get_products | 4 | 9 |
| `uz-autoloan-list` | get_products | get_products | 4 | 9 |
| `uz-microloan-list` | get_products | get_products | 4 | 9 |
| `uz-education-list` | get_products | get_products | 2 | 5 |
| `uz-credit-menu` | show_credit_menu | show_credit_menu | 1 | 3 |
| `uz-deposit-list` | get_products | get_products | 3 | 7 |
| `uz-card-open` | get_products | get_products | 2 | 5 |
| `uz-branch` | find_office | find_office | 1 | 3 |
| `uz-currency` | get_currency_info | get_currency_info | 1 | 3 |
| `uz-faq-escrow` | faq_lookup, none | — | 1 | 2 |
| `uz-faq-block-card` | faq_lookup, request_operator, none | — | 1 | 2 |
| `uz-faq-docs` | faq_lookup, none | — | 1 | 2 |
| `uz-office-pick` | select_office | select_office | 1 | 3 |
| `uz-recommend` | recommend_product, get_products | recommend_product | 1 | 3 |
| `uz-operator` | request_operator, none | request_operator | 1 | 3 |
| `uz-greeting` | — | — | 1 | 2 |

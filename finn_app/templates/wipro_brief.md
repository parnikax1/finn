# FINN brief — {{ brief.title }}

- Company: {{ brief.company }} (WIPRO)
- Filed: {{ brief.filed_at|filing_date }}
- Appointment effective: {{ brief.effective_date }}

{{ brief.appointment }}

{{ brief.context }}

## Evidence

{% for source in brief.pages -%}
- [{{ source.label }} · PDF p. {{ source.page }}]({{ brief.source_url }}#page={{ source.page }})
{% endfor -%}
- [Open original appointment PDF]({{ brief.source_url }})
{% if brief.release_url -%}
- [Open original press release PDF]({{ brief.release_url }})
{% endif %}

## Suggested question

{% if brief.contact and brief.contact.question -%}
**{{ brief.contact.name }} · Synthetic contact (fictional)**

{{ brief.contact.question }}
{% else -%}
No synthetic contact question is available.
{% endif %}

This is a FINN brief prepared for the dated walkthrough. The original PDFs are separate sources.

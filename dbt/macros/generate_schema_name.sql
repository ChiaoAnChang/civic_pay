{#
    Standard dbt override: when a model sets `+schema`, use it verbatim
    (`staging`, `marts`) instead of dbt's default `<target_schema>_<custom>`
    concatenation (`main_staging`). Models without a custom schema still fall
    back to the target's default schema.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}

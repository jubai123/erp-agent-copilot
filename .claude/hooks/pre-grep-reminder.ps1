$reminder = @'
**REMINDER: This project has a knowledge graph. Consider using code-review-graph MCP tools FIRST before exploring with Grep/Glob/Read.**
- semantic_search_nodes or query_graph instead of Grep
- get_impact_radius instead of manually tracing imports
- detect_changes + get_review_context instead of reading entire files
- query_graph with callers_of/callees_of/imports_of/tests_for for relationships
- get_architecture_overview + list_communities for architecture

Only use Grep/Glob/Read when the graph does not cover what you need.
'@

$json = @{
    hookSpecificOutput = @{
        hookEventName = 'PreToolUse'
        additionalContext = $reminder
    }
} | ConvertTo-Json -Depth 3 -Compress

Write-Output $json

"""Shared provider-agnostic LLM factory used by BOTH the mnesis core (broader
extraction providers) and the mnesis_agents LangGraph layer. Depends on neither —
langchain is imported lazily, so importing this module needs no extra installed.
"""

"""med -- the Cognitive Mediator.

A living interface between biological intent (a sentence) and digital execution
(tool/model calls), per the Cognitive_Mediator research spec:
    resolve   -> crystallize intent into a declarative JSON blueprint (FSM)
    dispatch  -> execute the blueprint, routing model steps to a local LLM (Ollama)
    verify    -> judge each step's result against its spec (pass/fail), not "trust me"
"""
__version__ = "0.1.0"

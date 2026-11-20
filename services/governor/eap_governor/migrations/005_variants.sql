-- BU variants: an agent that extends a published core agent (see eap_core/variants.py).
ALTER TABLE agents ADD COLUMN variant_of TEXT;
CREATE INDEX agents_variant_of ON agents (variant_of);

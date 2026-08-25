"""AD-1 — Budget et résultat de l'étape *retrouver* (commun à toutes les variantes)."""

from __future__ import annotations

from pydantic import Field, model_validator

from .document import Block, DomainModel


class RetrievalBudget(DomainModel):
    """Borne toute l'étape : appels modèle, nœuds, blocs, tokens, définitions et renvois inclus."""

    max_opens: int
    node_window: int
    search_limit: int
    max_llm_turns: int
    max_blocks: int | None = None
    max_tokens: int | None = None
    # Story 2.3 : les places réservées, **parmi** `max_opens`, aux nœuds que le profil désigne. Elle
    # vit ici et non dans `Settings` (revue coordonnée 2.3, A4) : c'est `max_opens` qu'elle borne, et
    # un appelant qui construit un budget réduit — évals, tests, mode économique — abaissait le quota
    # sans abaisser la réserve, si bien que la réserve devenait le quota entier. Les deux nombres se
    # lisent maintenant au même endroit, et le validateur ci-dessous interdit qu'ils se contredisent.
    profil_max_opens: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _la_reserve_ne_mange_pas_le_quota(self) -> RetrievalBudget:
        if self.profil_max_opens >= self.max_opens:
            # Une réserve égale au quota évincerait **tout** ce que la question a classé : le profil
            # ne serait plus un ordre mais un filtre, ce que la story interdit explicitement.
            raise ValueError(
                f"profil_max_opens ({self.profil_max_opens}) doit rester strictement inférieur à "
                f"max_opens ({self.max_opens}) : le profil ordonne, il ne remplace pas la question")
        return self


class RetrievalResult(DomainModel):
    blocs: list[Block] = Field(default_factory=list)
    opened_block_ids: list[str] = Field(default_factory=list)
    discarded_block_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


class NodeChild(DomainModel):
    """Un enfant directement navigable d'un nœud, sans contenu documentaire."""

    node_id: str
    title: str = ""


class NodeWindow(DomainModel):
    """Résultat d'`ouvrir_noeud` (AD-1) : fenêtre de blocs du nœud, `truncated` si le nœud dépasse `node_window`."""

    node_id: str
    title: str = ""
    children: list[NodeChild] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    truncated: bool = False
    next_cursor: int | None = None

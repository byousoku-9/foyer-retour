// Simulateur de salaire net luxembourgeois.
// Bareme officiel ACD 2025 (voir bareme.js). Parametres sociaux 2026.
// Resultat indicatif : il ne remplace pas une fiche de paie ni un calcul de l'ACD.

window.SIM = (function () {
  // Parametres sociaux. Modifiables depuis l'onglet Administration.
  // Sources : parametres sociaux IGSS valables au 01.06.2026 (indice 992,24),
  // salaire social minimum non qualifie 2 771,33 EUR par mois, maximum cotisable
  // 13 856,63 EUR par mois, abattement dependance 692,83 EUR par mois.
  // Taux de cotisation : loi du 18.12.2025 portant reforme des pensions.
  var P = {
    ssmAnnuel: 33255.96,       // 2 771,33 x 12
    plafondFacteur: 5,         // plafond cotisable = 5 x SSM
    tauxPension: 0.085,        // 8,50 % depuis le 01.01.2026 (contre 8,00 % avant)
    tauxMaladieNature: 0.028,
    tauxMaladieEspeces: 0.0025,
    tauxDependance: 0.014,     // sans plafond, apres abattement d'un quart de SSM
    fondsEmploi: 0.07,         // 9 % au-dela des seuils ci-dessous
    fondsEmploiTaux2: 0.09,
    seuilFondsClasse1: 150000,
    seuilFondsClasse2: 300000,
    impatrieTaux: 0.50,        // 50 % du brut exonere
    impatriePlafond: 400000,   // remuneration eligible plafonnee
    fraisObtention: 540,       // forfait annuel
    depensesSpeciales: 480     // forfait annuel
  };

  function params() { return P; }
  function setParams(patch) { Object.assign(P, patch); }

  // Impot brut selon le bareme officiel : taux x revenu - deduction.
  function impotBareme(revenuImposable, classe) {
    var table = window.BAREME_2025[classe];
    if (!table) throw new Error("Classe inconnue : " + classe);
    var R = Math.max(0, revenuImposable);
    for (var i = 0; i < table.length; i++) {
      var inf = table[i][0], sup = table[i][1], taux = table[i][2], ded = table[i][3];
      if (sup === null || R <= sup) return Math.max(0, taux * R - ded);
    }
    var last = table[table.length - 1];
    return Math.max(0, last[2] * R - last[3]);
  }

  // opts : { brut, classe: 'classe1'|'classe1a'|'classe2', impatrie: bool,
  //          mois: 12|13, forfaits: bool }
  function calcul(opts) {
    var brut = Math.max(0, Number(opts.brut) || 0);
    var classe = opts.classe || "classe1";
    var mois = Number(opts.mois) || 12;
    var forfaits = opts.forfaits !== false;

    var plafond = P.ssmAnnuel * P.plafondFacteur;
    var assietteePlafonnee = Math.min(brut, plafond);

    var tauxCotis = P.tauxPension + P.tauxMaladieNature + P.tauxMaladieEspeces;
    var cotisations = tauxCotis * assietteePlafonnee;
    var abattementDep = P.ssmAnnuel / 4;
    var dependance = P.tauxDependance * Math.max(0, brut - abattementDep);

    var netAvantImpot = brut - cotisations - dependance;

    // Exoneration impatrie : 50 % du brut, dans la limite de la remuneration eligible.
    var exoneration = 0;
    if (opts.impatrie) {
      exoneration = P.impatrieTaux * Math.min(brut, P.impatriePlafond);
    }

    var imposable = netAvantImpot - exoneration;
    if (forfaits) imposable -= (P.fraisObtention + P.depensesSpeciales);
    imposable = Math.max(0, imposable);

    var impot = impotBareme(imposable, classe);
    var seuil = (classe === "classe2") ? P.seuilFondsClasse2 : P.seuilFondsClasse1;
    var tauxFonds = imposable > seuil ? P.fondsEmploiTaux2 : P.fondsEmploi;
    var fonds = impot * tauxFonds;
    var impotTotal = impot + fonds;

    var netAnnuel = netAvantImpot - impotTotal;

    return {
      brut: brut,
      cotisations: cotisations,
      tauxCotisations: tauxCotis,
      dependance: dependance,
      netAvantImpot: netAvantImpot,
      exoneration: exoneration,
      imposable: imposable,
      impot: impot,
      fondsEmploi: fonds,
      tauxFondsEmploi: tauxFonds,
      impotTotal: impotTotal,
      netAnnuel: netAnnuel,
      netMensuel: netAnnuel / mois,
      mois: mois,
      tauxPrelevementGlobal: brut > 0 ? (brut - netAnnuel) / brut : 0
    };
  }

  // Taux fixes portés sur une fiche de retenue additionnelle, c'est-à-dire
  // sur le second salaire d'un ménage imposé collectivement, ou sur un second
  // emploi. Ils ne dépendent que de la classe d'impôt, jamais du revenu réel.
  // Ce sont des maxima : un taux réduit peut être demandé à l'administration.
  // Source : ACD, taux de retenue fixe inscrit sur la fiche additionnelle.
  var TAUX_FICHE_ADDITIONNELLE = { classe1: 0.33, classe1a: 0.21, classe2: 0.15 };

  // Ménage à deux salaires imposé collectivement.
  // Deux vues, qui ne donnent pas le même chiffre, et c'est normal :
  //   1. retenue : ce qui est prélevé chaque mois, barème sur le salaire
  //      principal et taux fixe sur le second ;
  //   2. regularisation : ce que le ménage doit réellement, barème de classe 2
  //      applique au revenu imposable cumulé, comme à la déclaration annuelle.
  function menage(opts) {
    var b1 = Math.max(0, Number(opts.brut1) || 0);
    var b2 = Math.max(0, Number(opts.brut2) || 0);
    var mois = Number(opts.mois) || 12;
    var classe = opts.classe || "classe2";
    var forfaits = opts.forfaits !== false;

    // Le salaire le plus élevé porte la fiche principale, par usage.
    var principal = Math.max(b1, b2);
    var secondaire = Math.min(b1, b2);

    var rP = calcul({ brut: principal, classe: classe, mois: mois, impatrie: opts.impatrie1, forfaits: forfaits });
    // Le second salaire subit les cotisations, puis un taux fixe, sans barème.
    var rS = calcul({ brut: secondaire, classe: classe, mois: mois, forfaits: false });
    var tauxFixe = TAUX_FICHE_ADDITIONNELLE[classe] || 0.15;
    var impotSecondaire = rS.netAvantImpot * tauxFixe;

    var retenueTotale = rP.impotTotal + impotSecondaire;
    var netAvantImpotMenage = rP.netAvantImpot + rS.netAvantImpot;
    var netRetenue = netAvantImpotMenage - retenueTotale;

    // Régularisation annuelle : barème appliqué au revenu imposable cumulé.
    var imposableCumule = Math.max(0, rP.imposable + rS.netAvantImpot - (forfaits ? (P.fraisObtention + P.depensesSpeciales) : 0));
    var impotAssiette = impotBareme(imposableCumule, classe);
    var seuil = (classe === "classe2") ? P.seuilFondsClasse2 : P.seuilFondsClasse1;
    var tauxFonds = imposableCumule > seuil ? P.fondsEmploiTaux2 : P.fondsEmploi;
    var impotAssietteTotal = impotAssiette * (1 + tauxFonds);
    var netReel = netAvantImpotMenage - impotAssietteTotal;

    return {
      brutPrincipal: principal,
      brutSecondaire: secondaire,
      brutMenage: principal + secondaire,
      cotisations: rP.cotisations + rP.dependance + rS.cotisations + rS.dependance,
      netAvantImpot: netAvantImpotMenage,
      tauxFixeSecondaire: tauxFixe,
      impotPrincipal: rP.impotTotal,
      impotSecondaire: impotSecondaire,
      retenueTotale: retenueTotale,
      netRetenue: netRetenue,
      netMensuelRetenue: netRetenue / mois,
      imposableCumule: imposableCumule,
      impotAssiette: impotAssietteTotal,
      netReel: netReel,
      netMensuelReel: netReel / mois,
      // Positif : le ménage devra un solde. Négatif : il sera remboursé.
      solde: impotAssietteTotal - retenueTotale,
      mois: mois
    };
  }

  // Comparaison des trois situations les plus utiles.
  function comparatif(brut, mois) {
    return [
      { label: "Classe 1", res: calcul({ brut: brut, classe: "classe1", mois: mois }) },
      { label: "Classe 1a", res: calcul({ brut: brut, classe: "classe1a", mois: mois }) },
      { label: "Classe 2", res: calcul({ brut: brut, classe: "classe2", mois: mois }) },
      { label: "Classe 2 + impatries", res: calcul({ brut: brut, classe: "classe2", mois: mois, impatrie: true }) }
    ];
  }

  // Capacite d'emprunt indicative.
  // opts : { netMensuel, chargesMensuelles, tauxAnnuel, annees, effort }
  function capaciteEmprunt(opts) {
    var net = Math.max(0, Number(opts.netMensuel) || 0);
    var charges = Math.max(0, Number(opts.chargesMensuelles) || 0);
    var taux = Number(opts.tauxAnnuel) || 0.04;
    var annees = Number(opts.annees) || 25;
    var effort = Number(opts.effort) || 0.33;

    var dispo = net * effort - charges;
    if (dispo <= 0) return { mensualiteDisponible: 0, capital: 0, dispo: dispo };

    var r = taux / 12, n = annees * 12;
    var capital = r === 0 ? dispo * n : dispo * (1 - Math.pow(1 + r, -n)) / r;
    return { mensualiteDisponible: dispo, capital: capital, taux: taux, annees: annees, effort: effort };
  }

  function mensualite(capital, tauxAnnuel, annees) {
    var r = tauxAnnuel / 12, n = annees * 12;
    if (r === 0) return capital / n;
    return capital * r / (1 - Math.pow(1 + r, -n));
  }

  return {
    calcul: calcul,
    menage: menage,
    tauxFicheAdditionnelle: TAUX_FICHE_ADDITIONNELLE,
    comparatif: comparatif,
    impotBareme: impotBareme,
    capaciteEmprunt: capaciteEmprunt,
    mensualite: mensualite,
    params: params,
    setParams: setParams
  };
})();

// Base du chat contrats : verdicts par sinistre extraits de conditions generales
// publiques du marche luxembourgeois (editions 2017 a 2023), assureurs anonymises.
// Genere depuis la case study MRH LU, ne pas editer a la main.
window.CONTRATS_KB = {
  "assureurs": [
    "Assureur A",
    "Assureur B",
    "Assureur C",
    "Assureur D"
  ],
  "scenarios": [
    {
      "groupe": "Vélo volé",
      "titre": "Dans l'appartement",
      "question": "Mon vélo électrique (2500 EUR) est volé à l'intérieur de mon appartement après effraction de la porte. Le vol est-il indemnisé, à quel plafond, quelle franchise, et le vélo a-t-il une sous-limite ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered_with_conditions",
          "plafond": "Capital déclaré aux Conditions Particulières",
          "franchise": "Sans franchise",
          "cle": "Le vol dans l’appartement après effraction de la porte est garanti, sous réserve que le vélo soit inclus comme « bien assuré » dans le contenu déclaré ; plafonds et conditions selon les Conditions Particulières.",
          "citation": "l'assureur garantit : 1.8.1.1 Vol, actes de vandalisme et de malveillance... Les dommages matériels accidentels biens assurés ... à immeuble assuré : Dans tous assuré doit être survenu dans une des circonstances suivantes : Par effraction, escalade et/ou usage de fausses clés... Contenu (sauf espèces, valeurs) Capital déclaré aux Conditions Particulières ... Sans franchise",
          "page": 40,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "sub_limited",
          "plafond": "2.500 € par sinistre",
          "franchise": "",
          "cle": "Le vol par effraction du contenu, incluant le vélo électrique, est couvert à l'intérieur de l'appartement, avec un plafond de 2.500 € par sinistre.",
          "citation": "l'assureur garantit, pour autant qu’une plainte ait été déposée auprès des autorités judiciaires ou de police compétentes : \n◼ Le vol ou la tentative de vol du contenu ou des valeurs situés dans le bâtiment désigné.",
          "page": 41,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "not_covered",
          "plafond": "",
          "franchise": "",
          "cle": "Le vélo électrique n'est pas considéré comme un 'cycle' au sens du contrat (seulement véhicules propulsés par énergie musculaire) et la garantie vol ne mentionne nulle part explicitement la couverture des vélos électriques à l'intérieur du logement.",
          "citation": "Par « cycle », on entend tout véhicule à deux roues au moins, et qui est propulsé exclusivement par l’énergie musculaire des personnes se trouvant sur ce véhicule, notamment à l’aide de pédales ou de manivelles.",
          "page": 59,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "sub_limited",
          "plafond": "10.000 € par objet et à concurrence du capital contenu choisi et figurant aux Dispositions Particulières pour le montant excédant ces 10.000 €",
          "franchise": "",
          "cle": "Le vélo volé à l’intérieur après effraction est couvert au titre du vol du contenu, avec une sous-limite de 10.000 € par objet.",
          "citation": "Vol et Vandalisme\nClassic\nComfort\nExcellent\nContenu\nÀ concurrence du capital figurant aux Dispositions Particulières\n...- autres biens mobiliers\n10.000 € par objet et à concur­rence de % de capital contenu choisi et figurant aux Dispositions Particulières pour le montant excédant ces 10.000 €",
          "page": 54,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Vélo volé",
      "titre": "Dans le local à vélos commun",
      "question": "Mon vélo électrique est volé dans le local à vélos / la cave commune de l'immeuble (une dépendance). Le vol dans une dépendance ou un local commun est-il couvert, à quelles conditions et quel plafond ?",
      "verdicts": {
        "Assureur D": {
          "statut": "sub_limited",
          "plafond": "7.500 EUR pour une dépendance ; 2.500 EUR pour une partie commune mise à disposition",
          "franchise": "Sans franchise",
          "cle": "Le plafond dépend de l'endroit exact du vol : 7.500 EUR dans une dépendance (cave privative), 2.500 EUR dans une partie commune mise à disposition (local à vélos commun), sous réserve du respect des conditions de sécurité (effraction, etc). Les objets de valeur ou espèces sont exclus dans ces locaux.",
          "citation": "Mobilier personnel situé dans les dépendances et les locaux non aménagés des immeubles en copropriété. 7.500 EUR [...] Mobilier personnel dans les parties communes mises à disposition des immeuble en copropriété. 2.500 EUR [...] Sans franchise",
          "page": 42,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "sub_limited",
          "plafond": "2.500 € par sinistre",
          "franchise": "",
          "cle": "Le vol est couvert dans une dépendance (local à vélos ou cave commune) uniquement si elle est équipée d'une serrure à cylindre ; exclusion si ce n'est pas le cas.",
          "citation": "Le vol ou la tentative de vol du contenu entreposé dans les annexes même non contiguës et équipées de serrures à cylindre. Cette intervention se fait sans application de la règle proportionnelle à concurrence de 2.500 € maximum par sinistre.",
          "page": 41,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "Le vol d'un vélo électrique dans une dépendance ou annexe est exclu s'il s'agit d'un objet de valeur ou d'argent.",
          "citation": "5.\ndes objets de valeur et de l’argent déposés dans les annexes et dépendances, ainsi que da",
          "page": 33,
          "verifiee": false
        },
        "Assureur C": {
          "statut": "sub_limited",
          "plafond": "limité au même montant de contenu qu’en dépendances",
          "franchise": "",
          "cle": "Le vol dans une dépendance ou cave commune est couvert à condition que les moyens de protection (portes/serrures) soient conformes ; toutefois, l’indemnisation est plafonnée au montant prévu pour le contenu des dépendances.",
          "citation": "Si la porte d’accès du garage, sous-sol ou véranda n’est pas conforme au niveau de protection exigé, il est admis que les moyens de protections requis soient sur la porte de communication entre ce local et l’habitation elle- même. Dans ce cas, la porte de communication est considérée comme une porte d’accès (elle doit être protégée comme telle) et le contenu des garages, sous-sols, vérandas est limité au même montant de contenu qu’en dépendances.",
          "page": 22,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Vélo volé",
      "titre": "Attaché dehors dans la rue",
      "question": "Mon vélo électrique est volé alors qu'il était attaché dans la rue devant chez moi, à l'extérieur de tout bâtiment. Le vol d'un bien à l'extérieur du domicile est-il couvert ou exclu ?",
      "verdicts": {
        "Assureur D": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "Le vol des moyens de mobilité douce (par ex. vélo électrique) déposés ou fixés à l'extérieur de l'immeuble assuré est explicitement exclu, sauf pour quelques biens très spécifiques (mobilier de jardin, etc.).",
          "citation": "Le vol des biens assurés, y compris les moyens de mobilité douce, qui sont déposés ou fixés à l'extérieur immeuble assuré ou dans les cours et jardins, sauf le mobilier de jardin, le matériel de jardinage motorisé, les plantations immeuble assuré.",
          "page": 41,
          "verifiee": true
        },
        "Assureur A": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "Le vol de biens se trouvant à l'extérieur, dans les cours, jardins ou passages d’accès (autres que mobilier de jardin, outillage ou plantations) est exclu.",
          "citation": "les vols des biens se trouvant à l’extérieur, dans les cours, les jardins -à l’exception du mobilier de jardin, de l’outillage motorisé ou non et des plantations situés à l’adresse du risque - dans les couloirs et passages d’accès, ainsi que dans les dépendances isolées ou contiguës avec ou sans communication intérieure avec la construction principale et non munies de serrure à cylindre ;",
          "page": 42,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "Les vols des objets déposés hors le Bâtiment dans les cours et jardins sont exclus.",
          "citation": "Les vols:\n3. des objets déposés hors le Bâtiment dans les cours et jardins ;",
          "page": 54,
          "verifiee": false
        },
        "Assureur C": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "Le vol d'un bien (ici, un vélo électrique) à l'extérieur du domicile, sans violence ou menace sur la personne, n'est pas couvert par ce contrat.",
          "citation": "Nous garantissons le vol ou la tentative de vol commis : • dans le bâtiment assuré ; ... • avec violence ou menaces sur la personne. Nous prenons en charge : • le vol du contenu ...",
          "page": 19,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Cambriolage",
      "titre": "Bijoux et ordinateur",
      "question": "Cambriolage avec effraction : vol de bijoux pour 3000 EUR et d'un ordinateur portable. Sont-ils indemnisés ? quel plafond pour les objets de valeur / bijoux, faut-il un coffre-fort ?",
      "verdicts": {
        "Assureur D": {
          "statut": "sub_limited",
          "plafond": "5.000 EUR, 10.000 EUR ou 20.000 EUR (selon Conditions Particulières) pour objets précieux/collections ; objets de valeur (bagages) 2.500 EUR ; voir détails",
          "franchise": "Sans franchise",
          "cle": "Pour les bijoux, s'ils dépassent 10.000 EUR pièce, ils doivent être enfermés dans un coffre-fort scellé ou similaire. Pour les objets de valeur, plafond spécifique, et plainte à la police sous 24h.",
          "citation": "GARANTIE BIENS PRÉCIEUX ET COLLECTIONS\nLIMITE DE GARANTIE (par sinistre)\nProfils Individuel ou Familial :\nSelon montant indiqué aux Conditions Particulières 5.000 EUR ou 10.000 EUR ou 20.000 EUR Sans franchise\n... Les bijoux d'une valeur unitaire supérieure à 10.000 EUR doivent être enfermés dans un coffre-fort qui doit être scellé au mur ou au sol... En cas de non-... 5.000 EUR (si la limite de garantie choisie est de 5.000 EUR), ou de 10.000 EUR (si la limite de garantie choisie est de 10.000 EUR)\n(page 92)\nVol, perte accidentelle, détérioration ou destruction des bagages (sauf espèces et objets de valeur) 7.500 EUR Sans franchise... espèces, objets de valeur avec agression 2.500 EUR Sans franchise (page 73)",
          "page": 92,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "sub_limited",
          "plafond": "Les objets personnels déplacés dans le cadre d’un séjour temporaire : 2.500 € par sinistre. Les objets de valeur sont couverts en valeur vénale, à moins qu'une valeur agréée existe.",
          "franchise": "",
          "cle": "Bijoux = objets de valeur : ils sont couverts, mais en valeur vénale sauf valeur agréée, et il existe probablement un plafond pour les bijoux/objets précieux qui n'est pas explicitement précisé ici. Pour les objets personnels dérobés lors d’un séjour temporaire hors domicile, le plafond est 2.500 €. Pour le domicile, il faut vérifier si les conditions particulières ont limité les montants pour les bijoux/objets précieux, ce qui n’est pas explicitement donné ici ; la présence d’un coffre-fort n'est pas exigée dans les extraits ci-dessus.",
          "citation": "Vol ou la tentative de vol du contenu ou des valeurs situés dans le bâtiment désigné. ... au vol par effraction dans un bâtiment situé partout dans le monde, d’objets personnels appartenant à l’Assuré et déplacés dans le cadre d’un séjour temporaire avec un maximum de 2.500€ par sinistre. ... les objets de valeur qui sont couverts en valeur vénale, à moins qu’une valeur n’ait expressément été agréée par les parties contractantes ;",
          "page": 41,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "not_found",
          "plafond": "",
          "franchise": "",
          "cle": "Aucune disposition spécifique sur le vol, le plafond pour les bijoux/objets de valeur, ou l'obligation d'un coffre-fort ne figure dans les extraits fournis.",
          "citation": "(Aucune information explicitement relative au vol, aux plafonds des objets de valeur/bijoux ou à l'obligation de coffre-fort n'est présente dans les pages citées ci-dessus.)",
          "page": null,
          "verifiee": false
        },
        "Assureur C": {
          "statut": "sub_limited",
          "plafond": "10-20-30-40 % indiqué aux Dispositions Particulières pour les objets de valeur/bijoux ; ordinateur portable = plafond général du contenu",
          "franchise": "Franchise indiquée aux dispositions particulières et/ou au tableau des garanties",
          "cle": "Les bijoux sont sub-limités (plafond spécifique objets de valeur), et indemnisés à leur prix d’achat s’ils ont moins de 2 ans sur présentation de la facture ; ordinateur portable couvert au titre du contenu.",
          "citation": "Objets de valeur: -bijoux, montres, pierres précieuses, objets en métal précieux massif (or, argent, vermeil et platine), sculptures, vases, tableaux, tapisseries, tapis, ivoires, armes anciennes, livres rares, fourrures, lithographies, poupées, pièces d’argenterie, verre et cristal, services, bibelots, dont la valeur unitaire ou de collection est supérieure à 1.000 € ... 10-20-30-40 % indiqué aux Dispositions Particulières ... Toutefois, les bijoux sont indemnisés à leur prix d’achat s’ils ont moins de 2 ans sur présentation de la facture d’achat d’origine montant de la facture",
          "page": 38,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Nomade",
      "titre": "Smartphone volé en vacances",
      "question": "Mon smartphone est volé dans un train pendant mes vacances, hors du domicile. La garantie des biens nomades couvre-t-elle le vol à l'extérieur, à quel plafond ?",
      "verdicts": {
        "Assureur D": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "La garantie Biens Nomades exclut explicitement la perte accidentelle et le vol des smartphones en dehors de l'immeuble assuré.",
          "citation": "l'assureur ne garantit pas :\n\n1) La perte accidentelle des téléphones portables (GSM), smartphones, laptop et tablettes.",
          "page": 46,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "10.000 EUR maximum par sinistre",
          "franchise": "",
          "cle": "Le matériel multimédia nomade (dont smartphone) est couvert dans le monde entier, notamment en vacances, sous un plafond de 10 000 EUR par sinistre.",
          "citation": "Dans le Monde Entier\n◼ Responsabilité civile vie privée,\n◼ Matériel multimédia nomade,\n◼ Voyage : annulation, bagages et assistance,\n◼ Dommages aux biens,\n◼ E-protection (limité à certains pays d’Europe selon le type de prestation).",
          "page": 5,
          "verifiee": false
        },
        "Assureur B": {
          "statut": "not_covered",
          "plafond": "",
          "franchise": "",
          "cle": "Le vol des smartphones (appareils nomades) à l'extérieur n'est pas mentionné comme couvert et même explicitement exclu de certaines garanties.",
          "citation": "les dommages causés aux appareils destinés de par leur conception à des fins ludiques, audiovisuelles (lecteur, enregistreur de musique, d’images inanimées ou non: p.ex. cd, dvd, avec ou sans disque dur), de localisation et de navigation, de télécommunication (organiser, pda, smartphone etc.);",
          "page": 37,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "not_found",
          "plafond": "",
          "franchise": "",
          "cle": "Aucun extrait des pages fournies ne mentionne la garantie ni les conditions d'indemnisation pour le vol de smartphone ou d'objet nomade à l'extérieur du domicile.",
          "citation": "(Aucune mention dans les extraits fournis d'une couverture pour le vol d'un smartphone ou d'un bien nomade hors du domicile)",
          "page": null,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Dégât des eaux",
      "titre": "Fuite qui gondole le parquet",
      "question": "Une fuite de ma machine à laver a inondé et gondolé mon parquet. Les dégâts au parquet sont-ils indemnisés ? franchise ? recherche de fuite prise en charge ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered",
          "plafond": "",
          "franchise": "Sans franchise",
          "cle": "Les dégâts au parquet suite à une fuite d'eau de la machine à laver sont couverts sans franchise, ainsi que les frais de recherche de fuite (dans la limite de 5.000 EUR, hors gel).",
          "citation": "l'assureur garantit :   Les dommages matériels accidentels causés aux biens assurés par :  les fuites d'eau et les débordements provenant (1) des conduites se trouvant à l'intérieur des immeubles assurés, (2) de tout appareil sanitaire, appareil à effet d'eau et de chauffage reliés à une conduite d'eau, d'adduction et de distribution d'eau... Frais de recherche de fuites... immeuble assuré... 5.000 EUR (hors gel) ... Sans franchise",
          "page": 26,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "Le dégât au parquet causé par une fuite de la machine à laver est indemnisé au titre de la garantie 'dégâts des eaux', sauf exclusions spécifiques non applicables au parquet. Les frais de recherche de fuite sur des conduites encastrées sont couverts, mais pas les dommages à la machine elle-même.",
          "citation": "3.1.4.1. Etendue de la garantie l'assureur assure les dommages matériels aux biens désignés contre les dégâts des... \n3.1.4.2.2. aux conduites, aux installations et appareils hydrauliques, aux tuyaux d’évacuation, aux boilers, chaudières, citernes, aquariums et matelas d’eau à l’origine du sinistre ; toutefois, les dommages aux conduites encastrées sont pris en charge par la Compagnie ;",
          "page": 39,
          "verifiee": false
        },
        "Assureur B": {
          "statut": "covered_with_conditions",
          "plafond": "selon option choisie : 50 %, 75 % ou 100 % de la somme assurée pour Bâtiment ou Risques Locatifs",
          "franchise": "",
          "cle": "Les dégâts des eaux sont couverts sauf exclusions spécifiques (non listées ici pour appareils ménagers), les frais de recherche de fuite sont garantis dans la limite du plafond choisi mais limités à certains éléments ; parquet inondé suite à fuite de machine à laver semble bien couvert.",
          "citation": "le recours des tiers, le chômage immobilier, les frais de déblais et/ou les frais de recherche des fuites, et les honoraires d’experts ; l'ensemble de ces risques pouvant être garanti selon 3 options: 50 % (option 1), 75 % (option 2) ou 100 % (option 3) de la somme assurée pour Bâtiment ou Risques Locatifs. En cas de sinistre, l’assuré choisira lui-même l’ordre d’épuisement de la somme ainsi assurée pour ces risques accessoires. 1.1.8 Formule Sécurité - Dégâts des Eaux – Extensions Suite à la réalisation de l'un des événements couverts... la Compagnie garantit: ... les frais de remise en état ou de remplacement des conduites non souterraines d’adduction, de distribution ou d’évacuation des eaux, des installations de chauffage central et de tous appareils fixes à effet d’eau jusqu’à concurrence de 70 EUR à l’indice 100 (indice de construction).",
          "page": 32,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "covered_with_conditions",
          "plafond": "Valeur de reconstruction à neuf à concurrence du capital indiqué aux Dispositions Particulières",
          "franchise": "",
          "cle": "Les dégâts au parquet suite à une fuite sont couverts sous la garantie 'Dégâts des Eaux', dans la limite du capital et sous réserve des exclusions générales ; les frais de recherche de fuite sont également garantis.",
          "citation": "Dégâts des Eaux et de Mazout ou de Gel\nClassic\nComfort\nExcellent\nBâtiment\nValeur de reconstruction à neuf à concurrence du capital indiqué aux Dispositions Particulières\nContenu excepté espèces, valeurs et collection\nSelon capital indiqué aux Dispositions Particulières\nFrais de recherche de fuites, ouverture et fermeture des murs à l’inté",
          "page": 52,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Dégât des eaux",
      "titre": "J'inonde le voisin du dessous",
      "question": "Ma fuite d'eau a endommagé le plafond et les meubles du voisin du dessous. Les dommages causés au voisin sont-ils couverts par ma responsabilité civile, à quel plafond ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered",
          "plafond": "",
          "franchise": "",
          "cle": "La responsabilité civile vie privée couvre les dommages matériels causés à des tiers au cours de la vie privée, donc les dommages causés au voisin par une fuite d'eau de l'assuré sont couverts.",
          "citation": "l'assureur garantit : Les conséquences pécuniaires de la responsabilité civile non contractuelle que peut encourir assuré à la suite de dommages corporels, matériels et immatériels causés à des tiers au cours de sa vie privée, sur base des articles 1382, 1383 et/ou 1385 du Code Civil.",
          "page": 52,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered",
          "plafond": "896.823 € maximum par sinistre au titre des dommages matériels et des dommages immatériels",
          "franchise": "",
          "cle": "La responsabilité civile immeuble couvre les dommages causés par des fuites d'eau à des tiers (le voisin) et intervient à hauteur de 896.823 € pour les dommages matériels et immatériels.",
          "citation": "l'assureur garantit la responsabilité civile qu’un Assuré pourrait encourir sur la base des articles 1382 à 1386 du Code civil, à l’égard d’un tiers, en raison de dommages causés par le fait : ... du bâtiment désigné (en ce compris ses hampes ou antennes) servant exclusivement d’habitation ; ... L’intervention de la Compagnie se fera à concurrence de : ... 896.823 € maximum par sinistre au titre des dommages matériels et des dommages immatériels.",
          "page": 51,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "covered",
          "plafond": "",
          "franchise": "",
          "cle": "La responsabilité civile de l’assuré couvre les dommages causés à des tiers par son habitation privée, incluant donc les dommages causés au voisin par une fuite d’eau.",
          "citation": "à raison des dommages causés par son habitation privée et son garage privé, toute résidence secondaire de plaisance occupée de façon passagère (à l'exclusion toutefois des caravanes de camping), ses jardins, vergers et prairies jusqu'à 50 a",
          "page": 47,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "covered",
          "plafond": "non précisé dans les extraits fournis",
          "franchise": "",
          "cle": "La responsabilité civile vie privée couvre les dommages causés à des tiers, comme un voisin, du fait de biens immobiliers assurés par le contrat, n'étant pas explicitement exclus.",
          "citation": "Les garanties sont acquises dans le monde entier, sauf pour la résidence secondaire et les terrains non bâtis appartenant au preneur, pour lesquels elles sont limitées à l’Europe.\n15.2 Garantie de base\nNous garantissons",
          "page": 23,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Incendie",
      "titre": "Départ de feu en cuisine",
      "question": "Un incendie parti de la cuisine a endommagé les murs, l'électroménager et le mobilier. Les dommages sont-ils indemnisés, en valeur à neuf ou vétusté déduite ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "Le sinistre (incendie ayant endommagé le mobilier et l'électroménager) est couvert, mais l'indemnisation s'effectue en valeur à neuf avec déduction de la vétusté spécifique à chaque type de bien.",
          "citation": "Mobilier personnel [...] Toute origine (sauf dommages électriques) Valeur à neuf, avec déduction de la vétusté excédant 30 % La vétusté est à dires [...] Dommages électriques Valeur de reconstitution à neuf au jour du sinistre, avec une déduction vétusté de 8% par an, avec un maximum de 80%.",
          "page": 108,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "Les dommages causés aux murs, à l’électroménager et au mobilier par un incendie sont couverts, indemnisés en valeur à neuf mais avec vétusté déduite pour certaines catégories (bâtiment, équipements électriques, mobilier), selon des modalités précises.",
          "citation": "Le bâtiment doit être assuré en valeur à neuf si l’Assuré est propriétaire ou en valeur réelle si l’Assuré est locataire. […] Les équipements électriques (ex : moteur de porte de garage) faisant partie intégrante du bâtiment désigné sont couverts en valeur à neuf, déduction faite d’une vétusté de 5% par année d’ancienneté révolue. [...] Le mobilier est assuré en valeur à neuf, excepté : le linge, les effets d’habillement qui sont couverts en valeur réelle ; le matériel multimédia, les équipements domotiques et les équipements d’alarme et de surveillance de plus de deux ans d’âge qui sont couverts en...",
          "page": 19,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "covered_with_conditions",
          "plafond": "au maximum au prix d’un objet neuf identique ou rendant un service identique avec des capacités et performances équivalentes au moment du sinistre sans toutefois dépasser la somme assurée pour Mobilier",
          "franchise": "",
          "cle": "L’indemnisation se fait en valeur à neuf, dans la limite du prix d’un objet neuf identique ou équivalent, sans dépasser la somme assurée pour le mobilier.",
          "citation": "La valeur à neuf correspond au maximum au prix d’un objet neuf identique ou rendant un service identique avec des capacités et performances équivalentes au moment du sinistre sans toutefois dépasser la somme assurée pour Mobilier.",
          "page": 37,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "covered_with_conditions",
          "plafond": "Selon capital indiqué aux Dispositions Particulières et limites du Tableau des Garanties et Franchises",
          "franchise": "Franchise éventuelle à déduire selon les dispositions particulières (non spécifiée ici)",
          "cle": "Indemnisation en valeur à neuf, mais vétusté déduite si supérieure à 30 % ; application de limites et franchise éventuelle.",
          "citation": "Si vos biens sont assurés en valeur à neuf, la vétusté sera entièrement déduite lorsqu’elle est supérieure à 30 %. [...] Pour calculer l’indemnité à partir du dommage, il faut en déduire éventuellement la vétusté, appliquer les limites d’intervention, et enfin déduire une franchise éventuelle. [...] L’indemnisation se fait en valeur de reconstruction à neuf. Toutefois la vétusté d’un bien ou de la partie sinistrée d’un bien est intégralement déduite dès qu’elle excède 30 %.",
          "page": 37,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Tempête",
      "titre": "Tuiles arrachées, infiltration",
      "question": "Une tempête a arraché des tuiles et la pluie s'est infiltrée, abîmant le plafond. Les dommages de tempête et l'infiltration sont-ils couverts, avec quelle franchise ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered",
          "plafond": "Valeur de reconstruction à neuf pour l’immeuble, capital déclaré pour le contenu",
          "franchise": "Sans franchise",
          "cle": "Dommages matériels à l’immeuble et au contenu consécutifs à une tempête sont couverts, sauf négligence ou défaut d’entretien.",
          "citation": "l'assureur garantit : Les dommages matériels accidentels causés aux biens assurés par une tempête, de la grêle ou le poids de la neige ou de la glace accumulée sur les toitures. En cas de sinistre couvert ci-dessus, l'assureur garantit en outre : les dommages matériels accidentels consécutifs causés immeuble assuré. ... OCCUPANT ... Immeuble assuré Valeur de reconstruction à neuf ... Sans franchise ... Contenu ... Capital déclaré aux Conditions Particulières ... Sans franchise.",
          "page": 24,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "Les dommages de tempête (tuiles arrachées) sont couverts sauf défaut d'entretien, mais le contrat exclut spécifiquement la prise en charge des dommages à la toiture elle-même (tuiles et étanchéité) dans la garantie dégâts des eaux ; par ailleurs, les dégâts d'infiltration du plafond causés directement par la tempête seraient couverts si la cause première était bien la tempête, si le bâtiment n'était pas mal entretenu.",
          "citation": "Ne sont toutefois pas couverts les dommages : ◼ résultant d’un défaut de réparation ou d’entretien du bâtiment désigné ; ◼ causés au contenu se trouvant dans une construction non préalablement endommagée par le vent de tempête , la grêle, la pression de la neige ou de la glace ; ...\n3.1.4.2.1. à la toiture du bâtiment ainsi qu’aux revêtements qui en assurent l’étanchéité ;",
          "page": 37,
          "verifiee": false
        },
        "Assureur B": {
          "statut": "not_found",
          "plafond": "",
          "franchise": "",
          "cle": "Aucune information sur la couverture ou l'exclusion des dommages de tempête ou d'infiltration dans les extraits fournis.",
          "citation": "Aucune mention dans les extraits des pages sur la couverture des dommages de tempête ou d'infiltration.",
          "page": null,
          "verifiee": false
        },
        "Assureur C": {
          "statut": "covered_with_conditions",
          "plafond": "Valeur de reconstruction à neuf à concurrence du capital indiqué aux Dispositions Particulières",
          "franchise": "Non précisée explicitement dans les extraits fournis, dépend du tableau des garanties et franchises mentionné à la page 51.",
          "cle": "La garantie de la pluie qui s'infiltre n'est acquise que si elle pénètre dans les 48 heures suivant la destruction totale ou partielle de la toiture par tempête.",
          "citation": "Le vent de tempête qui atteint une vitesse de pointe d’au moins 80 km/h attestée par la station du service météorologique et hydrographique national la plus proche du bâtiment assuré ou dont la force endommage des constructions présentant une résistance équivalente ... La pluie, la neige ou la grêle pénétrant à l’intérieur des bâtiments assurés dans les 48 heures suivant leur destruction totale ou partielle par l’action directe du vent, de la grêle ou de la neige sur la toiture.",
          "page": 10,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Bris de glace",
      "titre": "Plaque vitrocéramique fêlée",
      "question": "Ma plaque de cuisson vitrocéramique s'est fêlée. Le bris de glace couvre-t-il les plaques vitrocéramiques, inclus d'office ou en option ?",
      "verdicts": {
        "Assureur D": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "La garantie BRIS DE VITRES, GLACES ET MIROIRS ne cite pas les plaques de cuisson vitrocéramique parmi les biens assurés, uniquement les vitrages, glaces, miroirs, dômes, coupoles, panneaux translucides ou transparents en verre ou en matière plastique réputés immeubles.",
          "citation": "l'assureur garantit les biens suivants :\nImmeuble assuré\nLes parties vitrées : portes, fenêtres, fenêtres de toit, garde-corps et parois séparatives.\nLes glaces, miroirs, dômes, coupoles, panneaux translucides ou transparents, en verre ou en matière plastique réputés immeubles.\nLes parties vitrées des capteurs solaires et des installations photovoltaïques.\nLes enseignes lumineuses.",
          "page": 29,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered",
          "plafond": "",
          "franchise": "",
          "cle": "La garantie bris de vitrages couvre explicitement le bris accidentel des plaques vitrocéramiques ou à induction, sans indication de condition particulière ou d'option payante.",
          "citation": "l'assureur étend sans supplément de prime la portée de la garantie : à la prise en charge du bris accidentel des plaques vitrocéramiques ou à induction",
          "page": 40,
          "verifiee": false
        },
        "Assureur B": {
          "statut": "not_covered",
          "plafond": "",
          "franchise": "",
          "cle": "Le bris de glace ne couvre pas les plaques vitrocéramiques de cuisson dans le bâtiment ; il concerne la vitrerie, les vitrages, miroirs, briques de verre, mais la plaque de cuisson n'est pas mentionnée.",
          "citation": "Par stipulation expresse aux Conditions Particulières, peuvent également être assurés: la vitrerie artistique et façonnée (vitrages gravés, bombés, argentés, étamés, oxydés) ; les peintures et inscriptions ; les briques de verres, revêtements de façade et revêtements muraux en verre ; les serres à usage professionnel.",
          "page": 34,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "excluded",
          "plafond": "",
          "franchise": "",
          "cle": "Les plaques de cuisson vitrocéramiques ne sont pas explicitement incluses dans la garantie 'Dégâts au Vitrages', ni parmi les appareils couverts via une option ; les vitres d'appareils ménagers sont même exclues.",
          "citation": "• les dommages causés aux :\n\t - vitrages isolants de plus de 10 m²;\n\t - vitrages spéciaux (marbrites et marmorites, vitrages \t\t scellés sur pierre ou sur maçonnerie, vitrages collés) ;\n\t - objets en verre tels que bibelots, lustres, vaisselle ;\n\t - parties vitrées des appareils audiovisuels et multimédia;",
          "page": 12,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Valeur à neuf",
      "titre": "Mobilier de 5 ans détruit par le feu",
      "question": "Après un incendie, mon mobilier et mon électroménager ont 5 ans. Suis-je remboursé en VALEUR À NEUF (remplacement à l'identique sans déduction) ou en valeur vétusté déduite ? Y a-t-il un seuil d'âge ou un pourcentage de vétusté au-delà duquel on déduit ?",
      "verdicts": {
        "Assureur D": {
          "statut": "sub_limited",
          "plafond": "",
          "franchise": "",
          "cle": "Mobilier personnel et électroménager sont indemnisés en valeur à neuf, mais avec déduction de la vétusté excédant 30% pour le mobilier général ; les biens technologiques (électroménager) ont une vétusté de 75% si plus de 4 ans.",
          "citation": "Mobilier personnel [...] Toute origine (sauf dommages électriques) Valeur à neuf, avec déduction de la vétusté excédant 30 % La vétusté est à dires\n[...]\nBiens technologiques (sauf les appareils de commande à distance) [...] moins de deux ans = 20% moins de trois ans = 35% moins de quatre ans = 50% plus de quatre ans = 75% La vétusté à la valeur à neuf du matériel.",
          "page": 108,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "Le mobilier est couvert en valeur à neuf sauf pour certains biens (ex : linge ou matériel multimédia de plus de 2 ans), mais la vétusté n'est déduite que pour la partie qui excède 30% de la valeur à neuf.",
          "citation": "Le mobilier est assuré en valeur à neuf, excepté :\n◼ le linge, les effets d’habillement qui sont couverts en valeur réelle ;\n◼ le matériel multimédia, les équipements domotiques et les équipements d’alarme et de surveillance de plus de deux ans d’âge qui sont couverts en [...] La vétusté d’un bien sinistré ou de la partie sinistrée d’un bien sera déduite en cas d’assurance en valeur à neuf, pour la partie qui excède 30% de la valeur à neuf, cette proportion étant portée à 40% pour les sinistres affectant la garantie “tempête et grêle”.",
          "page": 19,
          "verifiee": false
        },
        "Assureur B": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "Le remboursement en valeur à neuf sans déduction de vétusté ne s'applique que si le mobilier et l'électroménager ont moins de 3 ans. Au-delà, une vétusté est déduite dès la première année suivant les 3 ans.",
          "citation": "les appareils assurés seront indemnisés en valeur à neuf, et sans déduction pour vétusté. L’indemnisation en valeur à neuf ne s’applique ni à ce matériel ni à ces appareils s'ils ont plus de trois (3) ans... A compter de la troisième année, le coefficient de vétusté sera déterminé et déduit dès la première année.",
          "page": 72,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "sub_limited",
          "plafond": "Vétusté intégralement déduite dès qu’elle excède 30%",
          "franchise": "",
          "cle": "La vétusté (dépréciation liée à l'âge) est entièrement déduite si elle dépasse 30%, même en valeur à neuf, sauf si l'appareil a moins de 2 ans et sur présentation d'une facture ; il n'y a donc pas de remboursement en valeur à neuf sans déduction au-delà de 2 ans et au-dessus de 30% de vétusté.",
          "citation": "Si vos biens sont assurés en valeur à neuf, la vétusté sera entièrement déduite lorsqu’elle est supérieure à 30 %... Toutefois la vétusté d’un bien ou de la partie sinistrée d’un bien est intégralement déduite dès qu’elle excède 30 %... Toutefois, les appareils de moins de 2 ans d’âge endommagés suite à un événement couvert au titre des garanties “Incendie et Événements assimilés” et “Dégâts des eaux” sont indemnisés sans déduction de vétusté sur présentation de la facture d’achat. Dans les autres cas, indemnisation selon les modalités ci-contre",
          "page": 37,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Responsabilité civile",
      "titre": "Enfant / animal cause un dommage",
      "question": "Ma responsabilité civile vie privée (mon enfant casse quelque chose chez un ami, mon chien mord un passant) est-elle INCLUSE d'office ou vendue en OPTION, et à quel plafond de garantie ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "La garantie Responsabilité Civile Vie Privée est vendue en option et doit être mentionnée aux conditions particulières ; elle couvre la responsabilité civile de l’assuré et des personnes dont il répond, y compris les enfants ou les animaux comme un chien, dans la vie privée.",
          "citation": "ATTENTION : LA GARANTIE DEFINIE CI-MENTIONNÉE AUX CONDITIONS PARTICULIÈRES. ELLE EST TOUJOURS ACQUISE SOUS RÉSERVE\n\n2.1 Garantie optionnelle : RESPONSABILITÉ CIVILE VIE PRIVÉE\n2.1.1 Ce que couvre la garantie RESPONSABILITÉ CIVILE VIE PRIVÉE l'assureur garantit :\n2.1.1.1 Principes généraux de la garantie RESPONSABILITÉ CIVILE VIE PRIVÉE Les conséquences pécuniaires de la responsabilité civile non contractuelle que peut encourir assuré à la suite de dommages corporels, matériels et immatériels causés à des tiers au cours de sa vie privée... Les pertes et dommages causés par des personnes dont assuré est civilement responsable en vertu de l'article 1384 du Code Civil, quelles que soient la nature et la gravité des fautes de ces personnes.",
          "page": 52,
          "verifiee": false
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "12.000.000 EUR maximum par sinistre au titre des dommages corporels ; 250.000 EUR maximum par sinistre au titre des dommages matériels et des dommages immatériels.",
          "franchise": "",
          "cle": "La garantie n'est acquise QUE si les conditions particulières du contrat mentionnent expressément l'option \"responsabilité civile vie privée\" comme souscrite.",
          "citation": "Les présentes conditions spéciales sont applicables si les conditions particulières mentionnent que la garantie “ responsabilité civile vie privée ” est accordée.",
          "page": 68,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "covered",
          "plafond": "1 500 000 EUR pour les dommages corporels, 125 000 EUR pour les dommages matériels",
          "franchise": "",
          "cle": "La responsabilité civile vie privée de l'assuré, incluant les dommages causés par ses enfants ou ses animaux domestiques dans le cadre de la vie privée, est couverte d'office, sauf exclusions expresses.",
          "citation": "l'assureur garantit la Responsabilité Civile de l’assuré: 1. à raison des dommages causés au cours de la vie privée à l’exclusion de toute activité professionnelle ; 2. à raison des dommages causés par ses enfants en leur qualité de simple particulier ... La garantie est limitée à la responsabilité civile découlant des qualités, relations de droit ou activités du preneur d’assurance et des autres assurés indiquées dans le contrat ... La limite d’intervention par sinistre de la Compagnie s’élève à 1 500 000 EUR pour les dommages corporels et à 125 000 EUR pour les dommages matériels...",
          "page": 47,
          "verifiee": false
        },
        "Assureur C": {
          "statut": "covered",
          "plafond": "850.000 €",
          "franchise": "",
          "cle": "La responsabilité civile vie privée est accordée d'office, sauf exclusions spécifiques, avec un plafond de 850.000 €. Sont par exemple exclus les dommages causés par la maladie d’animaux, sauf rage.",
          "citation": "15.1 Responsabilité civile vie privée\nOnt la qualité d’assuré : • le preneur d’assurance ... ainsi que leurs enfants ... ;\n... Les garanties sont acquises dans le monde entier, sauf pour la résidence secondaire ... pour lesquels elles sont limitées à l’Europe.\n15.2 Garantie de base\nNous garantissons\n...\nB2. dommages causés par ... eau : ... 850.000 € ...\nB3. objets confiés (séjour hôtel etc.) 850.000 € ...",
          "page": 23,
          "verifiee": false
        }
      }
    },
    {
      "groupe": "Sous-assurance",
      "titre": "Règle proportionnelle si contenu sous-estimé",
      "question": "Si j'ai déclaré une valeur de contenu inférieure à sa valeur réelle et qu'un gros sinistre survient, l'indemnité est-elle réduite au prorata (règle proportionnelle / sous-assurance) ? Existe-t-il une clause de renonciation à la règle proportionnelle ?",
      "verdicts": {
        "Assureur D": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "En cas de déclaration d'une valeur de contenu inférieure à sa valeur réelle (sous-assurance), l'indemnisation est réduite au prorata selon la règle proportionnelle de capitaux, sauf clause de renonciation qui n'est pas mentionnée ici.",
          "citation": "soit au rapport existant entre le capital déclaré et figurant aux Conditions Particulières et le capital réel au jour du sinistre (règle proportionnelle de capitaux)",
          "page": 137,
          "verifiee": true
        },
        "Assureur A": {
          "statut": "covered_with_conditions",
          "plafond": "500.000 € maximum par sinistre",
          "franchise": "",
          "cle": "La règle proportionnelle ne s'applique pas jusqu'à 500.000 € par sinistre, donc la sous-assurance ne réduit pas l'indemnité jusqu'à cette limite.",
          "citation": "L’éventuelle intervention de la Compagnie se fera sans application de règle proportionnelle à concurrence de 500.000 € maximum par sinistre.",
          "page": 47,
          "verifiee": true
        },
        "Assureur B": {
          "statut": "covered_with_conditions",
          "plafond": "",
          "franchise": "",
          "cle": "L'indemnité est réduite proportionnellement si la valeur assurée est inférieure à la valeur réelle (règle proportionnelle), sauf clause de renonciation qui ne figure pas dans les extraits fournis.",
          "citation": "Si la valeur assurée est inférieure à la valeur à neuf, l’indemnité sera réduite proportionnellement.",
          "page": 19,
          "verifiee": true
        },
        "Assureur C": {
          "statut": "covered",
          "plafond": "",
          "franchise": "",
          "cle": "La compagnie d’assurances renonce à l’application de la règle proportionnelle dans le cadre du contrat All.",
          "citation": "Article 35 : Abandon de la règle proportionnelle\nLa compagnie d’assurances renonce à l’application de la \nrègle proportionnelle dans le cadre du contrat All",
          "page": 41,
          "verifiee": false
        }
      }
    }
  ]
};

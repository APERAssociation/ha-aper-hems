# APER HEMS pour Home Assistant

Intégration Home Assistant des membres de l'[APER](https://aper-association.ch). Elle envoie chaque quart d'heure
les mesures de votre installation au Cockpit APER HEMS :

- production solaire, import et export réseau, charge et décharge de la batterie, niveau de batterie
  (la consommation de la maison est calculée par le Cockpit) ;
- les consommateurs que vous choisissez (pompe à chaleur, chauffe-eau, borne de recharge…).

Pendant que l'onglet **Live** de votre Cockpit est ouvert, les valeurs instantanées sont aussi transmises en direct.
C'est Home Assistant qui ouvre la connexion vers le Cockpit : aucun accès à votre Home Assistant n'est donné,
et il n'a pas besoin d'être accessible depuis Internet.

Les trous (Home Assistant éteint, coupure Internet) sont comblés automatiquement à partir de l'historique de
Home Assistant, jusqu'à 45 jours en arrière.

## Prérequis

- Être membre de l'APER, avec un compte actif sur [aper-association.ch](https://aper-association.ch).
- Une **clé API** personnelle, fournie par l'APER.
- Des capteurs de puissance (W, kW) ou des compteurs d'énergie (Wh, kWh) dans Home Assistant.

## Installation

### Avec HACS

1. HACS → menu ⋮ → **Dépôts personnalisés** → ajoutez `https://github.com/APERAssociation/ha-aper-hems`,
   catégorie **Intégration**.
2. Recherchez **APER HEMS**, installez, puis redémarrez Home Assistant.

### À la main

Copiez le dossier `custom_components/aper_hems` dans le dossier `custom_components` de votre configuration
Home Assistant, puis redémarrez Home Assistant.

## Configuration

1. **Paramètres → Appareils et services → Ajouter une intégration → APER HEMS**.
2. Renseignez :
   - **Réseau (W)** : positif = import, négatif = export (décochez « Positif = import réseau » si c'est l'inverse) ;
   - **Production solaire (W)**, **Batterie (W)** (positif = charge) et **état de charge (%)**, si vous en avez ;
   - les **compteurs d'énergie (kWh)**, optionnels mais plus précis que les capteurs de puissance ;
   - votre **clé API**.
3. Pour ajouter, modifier ou supprimer des consommateurs : bouton **Configurer** de l'intégration.
   Les « entités à soustraire » évitent de compter deux fois un appareil déjà mesuré par un autre capteur.

Les réglages se modifient ensuite par **⋮ → Reconfigurer**.

## Données envoyées

Uniquement les valeurs des capteurs choisis et la météo de votre domicile, agrégées par quart d'heure, vers `aper-association.ch`, avec votre clé API. Votre position ne quitte Home Assistant que vers Open-Meteo, arrondie à environ 1 km, pour obtenir la météo.
Aucune autre donnée de Home Assistant n'est transmise.

Météo : données [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0), demandées par votre Home Assistant.

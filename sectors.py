# -*- coding: utf-8 -*-
import re

SECTORS = {
"Couverture & toiture": [
 "Waterproofing service",
 "Couvreur","Entreprise de toiture","Entreprise d'étanchéité"],
"Bâtiment – gros œuvre & construction": [
 "Maçon","Entreprise de construction","Constructeur de maisons personnalisées","Constructeur de terrasses",
 "Entreprise de terrassement","Société de travaux publics","Société de construction de piscine","Travaux généraux",
 "Entrepreneur","Ingénieur géotechnique","Service de location de bennes à déchets","Service de réparation de machines"],
"Bâtiment – second œuvre & rénovation": [
 "Peintre en bâtiment","Carreleur","Menuisier","Atelier de menuiserie","Fournisseur de fenêtres","Spécialiste de la salle de bains",
 "Entrepreneur spécialisé dans les revêtements de sol","Plâtrier.ère","Service de pose de parquet","Vitrerie - miroiterie",
 "Entrepreneur en construction intérieure","Magasin de revêtements de sol","Décorateur d'intérieur","Magasin de poêles à bois"],
"Plomberie, chauffage, électricité & énergie": [
 "Chauffage",
 "Plombier","Électricien","Chauffagiste","Entrepreneur spécialisé dans les systèmes de CVC","Fournisseur de systèmes de climatisation",
 "Service de réparation de systèmes de climatisation","Entrepreneur en énergie solaire","Entreprise d'énergie solaire",
 "Fournisseur d'équipements pour l'adoucissement de l'eau","Service d'assainissement","Ramoneur","Entretien d'antennes",
 "Service de réparation d'appareils électroménagers"],
"Serrurerie & sécurité": [
 "Détective privé",
 "Serrurier","Service de serrurerie d'urgence","Service de reproduction de clés","Agence de sécurité",
 "Installateur de systèmes de sécurité","Private investigator"],
"Nettoyage, entretien & services à domicile": [
 "House cleaning service",
 "Service de nettoyage et d'entretien","Services de nettoyage","Service de ménage","Entreprise de nettoyage de vitres","Laveur de vitres",
 "Service de nettoyage de piscines","Service de débarras de maison","Service d'extermination des nuisibles"],
"Espaces verts & extérieur": [
 "Service d'élagage","Paysagiste","Jardinier","Pépinière","Service agricole","Magasin de matériel pour piscines"],
"Transport, taxi & déménagement": [
 "Service de taxi","Service de chauffeur privé","Service de chauffeur particulier","Service de transport","Service de limousine",
 "Service de taxi minibus","Station de taxis","Service de livraison","Entreprise de déménagement","Services de déménagement et de stockage",
 "Agence de location de fourgonnettes","Service de remorquage","Bergingsbedrijf"],
"Automobile": [
 "Magasin de pneus", "Atelier de réparation pour motos",
 "Service d'esthétique automobile","Garage automobile","Auto-école","Service de réparation de pare-brise","Atelier de réparation automobile",
 "Atelier de réparation de pneus","Casse automobile","Centre de contrôle technique","Mécanicien","Prestataire de tuning automobile",
 "Vendeur de voitures d'occasion","Vente aux enchères de voitures"],
"Immobilier, diagnostics & conciergerie": [
 "Property management company", "Real estate agency",
 "Agence immobilière","Agent immobilier","Consultant immobilier","Société de gestion immobilière","Expert immobilier",
 "Gestionnaire de patrimoine immobilier","Investissement immobilier","Centre de diagnostic","Inspecteur en bâtiment","Conciergerie privée"],
"Restauration": [
 "Restaurant vietnamien",
 "Pizzeria","Restaurant","Kebab","Restaurant de hamburgers","Restaurant français","Restaurant africain",
 "Restaurant de spécialités du sud-ouest de la France","Restaurant de type buffet","Restaurant de viande","Restaurant gastronomique",
 "Restaurant indien","Restaurant italien","Restaurant libanais","Restauration rapide","Livraison de pizza à domicile","Traiteur",
 "Vietnamese restaurant"],
"Hébergement & tourisme": [
 "Chambre d'hôtes","Hôtel","Location d'appartement de vacances","Agence de location d'appartements","Agence de location de maisons de vacances",
 "Maison de vacances","Résidence hôtelière","Terrain de camping"],
"Beauté & coiffure": [
 "Beauty Salon", "Hairdresser",
 "Salon de coiffure","Institut de beauté","Esthéticien","Service d'esthétique","Barbier","Salon de manucure","Centre d'épilation laser",
 "Magasin de perruques","Parfumerie","Boutique de santé et beauté"],
"Santé & paramédical": [
 "Ostéopathe","Chiropracteur","Kinésithérapeute","Cabinet infirmier","Nutritionniste","Chirurgien esthétique","Chirurgien plasticien",
 "Service de soins à domicile","Aide à domicile","Magasin de matériel orthopédique","Service de garde d'enfants à domicile"],
"Bien-être, massage & thérapies alternatives": [
 "Spa mát xa",
 "Centre de bien-être","Institut de massages","Massothérapeute","Kinésiologue","Praticien de médecine alternative","Centre de yoga",
 "Gestalt-thérapeute","Praticien en médecine holistique","Psychanalyste","Réflexologue","Service d'hypnothérapie","Sexologue",
 "Thérapeute psychosocial","Salon de massage thaïlandais","École de massage","Institut de spa et bien-être","Services de voyance",
 "Magasin de vitamines et compléments alimentaires"],
"Conseil, formation & services aux entreprises": [
 "Avocat","Cabinet d'avocats","Courtier d'assurances","Centre de coaching","Coach de vie","Centre de formation","Agence d'orientation professionnelle",
 "Agence de marketing","Assistance et services informatiques","Services aux entreprises","Sécurité et santé au travail","Service de soutien scolaire",
 "Entraîneur vocal","Service d'impression numérique","Magasin d'enseignes lumineuses"],
"Événementiel, photo & création": [
 "Photographe","Service de photographie","Service de drone","Service de location de matériel audiovisuel","Artist","Artiste",
 "Organisateur de mariages","Location de château gonflable","Service de fête pour enfants","Atelier de réparation d'instruments de musique"],
"Sport, loisirs & animaux": [
 "Centre d'escalade","Centre équestre","Salle de boxe thaïlandaise","Salle de fitness","Dresseur pour chiens","Éleveur de chiens","Pet groomer",
 "Service de réparation de bateaux"],
"Commerce de détail & divers": [
 "Scierie", "Bijouterie fantaisie",
 "Marchand d'or","Service de rachat de bijoux","Boutique de cigarettes électroniques","Dépôt-vente","Papeterie","Vêtements pour enfants",
 "Épicerie italienne","Miellerie","Boutique de lingerie","Magasin d'articles africains","Magasin d'électronique","Magasin de matériel de cuisine",
 "Magasin de meubles de bureau","Magasin de vélos"],
}
CAT2SECTOR = {c: s for s, cs in SECTORS.items() for c in cs}

def sector_of(cat):
    if not cat:
        return "Autres / non classé"
    return CAT2SECTOR.get(cat, "Autres / non classé")

_POSTAL_CITY = re.compile(r"\b(\d{5})\s*[,\s]\s*([^,]+?)\s*$")
_CITY_POSTAL = re.compile(r"^(.*?),\s*(\d{5})\s*,\s*(.+)$")

def parse_address(addr):
    """-> (code_postal, ville). Gère '..., 31190, Miremont' et '... 33430 Aubiac'."""
    if not addr:
        return None, None
    a = addr.strip().strip(",")
    parts = [p.strip() for p in a.split(",") if p.strip()]
    # forme 'rue, 31190, Ville'
    for i, p in enumerate(parts):
        if re.fullmatch(r"\d{5}", p) and i + 1 < len(parts):
            return p, parts[i + 1].title() if parts[i + 1].isupper() else parts[i + 1]
    # forme '..., 33430 Aubiac'
    m = re.search(r"(\d{5})\s+([^,\d][^,]*)$", a)
    if m:
        city = m.group(2).strip()
        return m.group(1), city.title() if city.isupper() else city
    m = re.search(r"\b(\d{5})\b", a)
    cp = m.group(1) if m else None
    city = parts[-1] if parts else None
    if city and re.search(r"\d", city) and cp:
        city = None
    return cp, city

def departement(cp):
    if not cp:
        return None
    if cp[:2] in ("20",):
        return "2A/2B"
    if cp[:2] in ("97", "98"):
        return cp[:3]
    return cp[:2]

"""Base de estrategias: metaclase con validación y auto-registro.

Requisito del enunciado: "una metaclase con responsabilidad concreta".
La responsabilidad de StrategyMeta es doble y muy concreta:

1. VALIDAR EL CONTRATO en tiempo de definición de clase (import-time):
   toda estrategia debe implementar generate_signals() y declarar un
   atributo `name`. Si no => TypeError ANTES de que el programa
   arranque. Fail-fast extremo: un módulo mal formado ni se importa.

2. AUTO-REGISTRO: cada estrategia válida se apunta sola en
   STRATEGY_REGISTRY. Añadir una estrategia nueva al sistema =
   definir su clase. Nada más: ni editar listas, ni tocar el backend
   (el endpoint /signals de FastAPI leerá este registro).
   Principio open/closed: abierto a extensión, cerrado a modificación.

CARNE DE ENTREVISTA — cuándo corre cada cosa:
- metaclase.__new__  -> al DEFINIR la clase (import del módulo)
- Strategy.__init__  -> al INSTANCIAR un objeto
La validación en import-time es la ventaja frente a alternativas:
un ABC con @abstractmethod falla al instanciar; la metaclase falla
al importar. Cuanto antes explota, más barato es el arreglo.
"""

import pandas as pd

# Registro global: nombre -> clase de estrategia.
STRATEGY_REGISTRY: dict[str, type] = {}


class StrategyMeta(type):
    """Metaclase: fabrica clases de estrategia validadas y registradas.

    type es la metaclase por defecto de Python: toda clase ES una
    instancia de type. Heredar de type y sobreescribir __new__ nos da
    un gancho en el momento exacto en que la clase se construye.
    """

    def __new__(mcs, name: str, bases: tuple, namespace: dict) -> type:
        # mcs=la metaclase, name=nombre de la clase en construcción,
        # bases=sus padres, namespace=dict con TODO lo definido en su
        # cuerpo (métodos, atributos). La clase aún no existe: estamos
        # decidiendo cómo construirla.
        cls = super().__new__(mcs, name, bases, namespace)

        # La clase base (sin padres con esta metaclase) no se valida
        # ni se registra: es la plantilla, no una estrategia.
        if not bases:
            return cls

        # --- Responsabilidad 1: validar el contrato ---
        if "generate_signals" not in namespace or not callable(
            namespace["generate_signals"]
        ):
            raise TypeError(
                f"La estrategia '{name}' debe implementar generate_signals(). "
                "Definida sin él, el sistema la rechaza en import-time."
            )
        if not isinstance(namespace.get("name"), str):
            raise TypeError(
                f"La estrategia '{name}' debe declarar un atributo de clase "
                "'name' (str) para el registro y la API."
            )

        # --- Responsabilidad 2: auto-registro ---
        strategy_name = namespace["name"]
        if strategy_name in STRATEGY_REGISTRY:
            raise TypeError(
                f"Nombre de estrategia duplicado: '{strategy_name}' ya está "
                f"registrado por {STRATEGY_REGISTRY[strategy_name].__qualname__}."
            )
        STRATEGY_REGISTRY[strategy_name] = cls
        return cls


class Strategy(metaclass=StrategyMeta):
    """Clase base de todas las estrategias.

    Contrato: generate_signals recibe un DataFrame de velas con
    columnas [open, high, low, close, volume] indexado por tiempo, y
    devuelve una Series alineada con valores {-1: corto/vender,
    0: fuera, +1: largo/comprar}.

    Las subclases NO llaman a super().generate_signals(): esta base
    existe para dar el contrato y la metaclase, no comportamiento.
    """

    name: str = ""  # las subclases lo sobreescriben (validado por la metaclase)

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        raise NotImplementedError  # la metaclase impide llegar aquí


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Demo:  python -m src.strategies.base
    # 1) Una estrategia bien formada se registra sola.
    # 2) Una mal formada explota AL DEFINIRSE, no al usarse.
    # --------------------------------------------------------------

    class EstrategiaBuena(Strategy):
        name = "buena"

        def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
            return pd.Series(0, index=candles.index)

    print(f"Registro tras definir EstrategiaBuena: {list(STRATEGY_REGISTRY)}")

    # Intentamos definir una estrategia SIN generate_signals.
    # Ojo: el error salta en la LÍNEA 'class ...', no al instanciar.
    try:

        class EstrategiaRota(Strategy):
            name = "rota"
            # sin generate_signals

    except TypeError as exc:
        print(f"Definición rechazada en import-time: {exc}")

    print(f"Registro final (la rota no entró): {list(STRATEGY_REGISTRY)}")

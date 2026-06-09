import enum
from sqlalchemy import Column, BigInteger, String, Integer, Text, Enum as SAEnum
from sqlalchemy import DateTime
from database import Base


class EstadoRegHor(str, enum.Enum):
    entrada_pendiente = "entrada pendiente"
    salida_pendiente = "salida pendiente"
    completo = "completo"


class OrigenRegHor(str, enum.Enum):
    api = "api"
    webapp = "webapp"
    agenteIA = "agenteIA"


class RegistroTiempo(Base):
    __tablename__ = "ws_registros_tiempo"

    id = Column(BigInteger, primary_key=True)
    upn_empleado = Column(String(255), nullable=False)
    entrada = Column(DateTime(timezone=True), nullable=False)
    salida = Column(DateTime(timezone=True), nullable=True)
    segundos_trabajados = Column(Integer, nullable=True)
    estado = Column(SAEnum(EstadoRegHor, name="estado_reghor", values_callable=lambda x: [e.value for e in x]), nullable=False, default=EstadoRegHor.salida_pendiente)
    origen = Column(SAEnum(OrigenRegHor, name="origen_reghor", values_callable=lambda x: [e.value for e in x]), nullable=False, default=OrigenRegHor.webapp)
    comentario_justificacion = Column(Text, nullable=True)
    upn_alta = Column(String(255), nullable=False)
    upn_ultima_modificacion = Column(String(255), nullable=False)
    fecha_alta = Column(DateTime(timezone=True), nullable=False)
    fecha_ultima_modificacion = Column(DateTime(timezone=True), nullable=False)
